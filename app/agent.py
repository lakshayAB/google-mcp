"""Gemini function-calling loop that answers questions with the Google Ads MCP tools.

Write tools (labels, assets, video upload) use the MCP server's propose-and-confirm flow:
a call with validate_only=True only checks the change and returns a checksum, and a second
call with validate_only=False and that checksum makes it. The model may run the first step
freely, but every committing call is held here as a pending action and only runs after the
user clicks Confirm in the chat (see `resume`).
"""

import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from google import genai
from google.genai import errors, types

from app.config import UPLOAD_DIR, Settings
from app.mcp_client import WRITE_CALL_TIMEOUT_SECONDS, GoogleAdsMCPClient
from app.prompts import build_system_prompt

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 10
MAX_OUTPUT_TOKENS = 16000
PENDING_TTL_SECONDS = 30 * 60
MAX_REMEMBERED_PROPOSALS = 200
# Tool arguments that name a file on the server. Only files attached in the chat are allowed.
FILE_PATH_ARGS = ("image_asset_path", "video_path")
CANCELLED_MESSAGE = "The user cancelled this change. Nothing was changed in Google Ads."
ABANDONED_MESSAGE = (
    "Not run: the user sent a new message instead of confirming. Nothing was changed in Google Ads."
)


class AgentError(RuntimeError):
    """An error to show the user, with an HTTP status code for the API response."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ChatResult:
    answer: str
    history: list[dict]
    # Set when the model wants to commit a change: shown to the user with Confirm / Cancel.
    pending_action: dict | None = None


@dataclass
class _PendingAction:
    id: str
    calls: list[types.FunctionCall]
    # Responses for the calls that already ran (reads, validations), by position in `calls`.
    done: dict[int, types.Part]
    # Positions in `calls` that wait for the user's confirmation.
    held: list[int]
    created: float = field(default_factory=time.monotonic)


class GeminiAgent:
    def __init__(self, settings: Settings):
        self._settings = settings
        # vertexai=False: always use the Gemini Developer API (AI Studio key), even though
        # GOOGLE_PROJECT_ID is set for the MCP server.
        self._client = genai.Client(api_key=settings.gemini_api_key, vertexai=False)
        self._pending: dict[str, _PendingAction] = {}
        # checksum -> change_items from validate_only=True proposals, to show on the confirm card.
        self._proposals: OrderedDict[str, list[dict]] = OrderedDict()

    async def chat(self, message: str, history: list[dict], mcp: GoogleAdsMCPClient) -> ChatResult:
        """Answer one user message using the MCP session."""
        contents = _load(history)
        # A change was waiting for confirmation but the user typed a new message instead.
        dangling = _trailing_calls(contents)
        if dangling:
            contents.append(
                types.Content(role="user", parts=[_response_part(c, ABANDONED_MESSAGE, True) for c in dangling])
            )
        _append_user_text(contents, message)
        return await self._loop(contents, mcp)

    async def resume(
        self, action_id: str, approve: bool, history: list[dict], mcp: GoogleAdsMCPClient
    ) -> ChatResult:
        """Run (approve=True) or cancel the held change, then let the model continue."""
        self._expire_pending()
        action = self._pending.pop(action_id, None)
        if action is None:
            raise AgentError("This change is no longer waiting for confirmation. Ask for it again.", 409)

        contents = _load(history)
        if _calls_key(_trailing_calls(contents)) != _calls_key(action.calls):
            raise AgentError("This change doesn't match the current conversation. Ask for it again.", 409)

        parts = dict(action.done)
        for i in action.held:
            call = action.calls[i]
            if approve:
                text, is_error = await self._call(mcp, call, timeout=WRITE_CALL_TIMEOUT_SECONDS)
                logger.info("Confirmed change %s: %s (error=%s)", action.id, call.name, is_error)
            else:
                text, is_error = CANCELLED_MESSAGE, True
                logger.info("Cancelled change %s: %s", action.id, call.name)
            parts[i] = _response_part(call, text, is_error)
        contents.append(types.Content(role="user", parts=[parts[i] for i in range(len(action.calls))]))

        if not approve:
            return ChatResult("Cancelled. Nothing was changed in Google Ads.", _dump(contents))
        return await self._loop(contents, mcp)

    async def _loop(self, contents: list[types.Content], mcp: GoogleAdsMCPClient) -> ChatResult:
        config = types.GenerateContentConfig(
            system_instruction=build_system_prompt(date.today(), self._settings.google_ads_default_customer_id),
            tools=[types.Tool(function_declarations=_function_declarations(mcp))],
            # We run the loop ourselves so every tool call goes through the MCP session.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )

        for iteration in range(1, MAX_ITERATIONS + 1):
            response = await self._generate(contents, config)

            if not response.candidates:
                reason = response.prompt_feedback.block_reason if response.prompt_feedback else None
                return ChatResult(
                    f"Gemini blocked this request ({reason or 'no reason given'}). Try rephrasing.", _dump(contents)
                )

            candidate = response.candidates[0]
            finish = candidate.finish_reason
            logger.info("Gemini iteration %d: finish_reason=%s", iteration, finish)
            if candidate.content and candidate.content.parts:
                contents.append(candidate.content)

            calls = response.function_calls
            if calls:
                parts, pending = await self._run_tools(mcp, calls)
                if pending is not None:
                    answer = _text_of(candidate) or "Please confirm this change."
                    return ChatResult(answer, _dump(contents), pending)
                contents.append(types.Content(role="user", parts=parts))
                continue

            text = _text_of(candidate)
            if finish == types.FinishReason.MAX_TOKENS:
                text += "\n\n_(The answer was cut off because it hit the length limit.)_"
            elif not text and finish not in (None, types.FinishReason.STOP):
                text = f"Gemini stopped without an answer (reason: {finish}). Try rephrasing the question."
            return ChatResult(text or "(No answer was returned.)", _dump(contents))

        return ChatResult(
            f"I stopped after {MAX_ITERATIONS} steps without finishing. "
            "Try a more specific question (one account, one date range, fewer metrics).",
            _dump(contents),
        )

    async def _run_tools(
        self, mcp: GoogleAdsMCPClient, calls: list[types.FunctionCall]
    ) -> tuple[list[types.Part], dict | None]:
        """Run the calls, holding back any that would commit a change.

        Returns (response parts, None) when everything ran, or ([], pending action for the UI)
        when at least one call needs the user's confirmation.
        """
        done: dict[int, types.Part] = {}
        held: list[int] = []
        for i, call in enumerate(calls):
            if _needs_confirmation(mcp, call):
                held.append(i)
                continue
            text, is_error = await self._call(mcp, call)
            if not is_error and mcp.is_write_tool(call.name):
                self._remember_proposal(text)
            done[i] = _response_part(call, text, is_error)

        if not held:
            return [done[i] for i in range(len(calls))], None

        self._expire_pending()
        action = _PendingAction(id=uuid.uuid4().hex, calls=list(calls), done=done, held=held)
        self._pending[action.id] = action
        logger.info("Holding change %s for confirmation: %s", action.id, [calls[i].name for i in held])
        return [], {"id": action.id, "changes": [self._describe(calls[i]) for i in held]}

    async def _call(
        self, mcp: GoogleAdsMCPClient, call: types.FunctionCall, timeout: float | None = None
    ) -> tuple[str, bool]:
        args = dict(call.args or {})
        problem = _file_arg_problem(args)
        if problem:
            return problem, True
        return await mcp.call_tool(call.name, args, timeout=timeout)

    def _remember_proposal(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        if isinstance(data, dict) and data.get("status") == "PROPOSED" and data.get("checksum"):
            self._proposals[str(data["checksum"]).lower()] = data.get("change_items") or []
            while len(self._proposals) > MAX_REMEMBERED_PROPOSALS:
                self._proposals.popitem(last=False)

    def _describe(self, call: types.FunctionCall) -> dict:
        """What the confirm card shows for one held call."""
        args = dict(call.args or {})
        checksum = str(args.get("checksum") or "").lower()
        shown = {k: v for k, v in args.items() if k not in ("checksum", "validate_only") and v is not None}
        for key in FILE_PATH_ARGS:
            if key in shown:
                shown[key] = Path(str(shown[key])).name
        return {
            "tool": call.name,
            "change_items": self._proposals.get(checksum),
            "arguments": shown,
        }

    def _expire_pending(self) -> None:
        cutoff = time.monotonic() - PENDING_TTL_SECONDS
        for key in [k for k, a in self._pending.items() if a.created < cutoff]:
            del self._pending[key]

    async def _generate(self, contents: list[types.Content], config: types.GenerateContentConfig):
        model = self._settings.gemini_model
        try:
            return await self._client.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
        except errors.ClientError as exc:
            detail = exc.message or str(exc)
            if exc.code == 429:
                raise AgentError(
                    "Gemini free-tier rate limit reached. Wait a minute and try again, "
                    f"or switch GEMINI_MODEL to another free model. ({detail})",
                    429,
                ) from exc
            if exc.code == 404:
                raise AgentError(f"Gemini model {model!r} was not found. Check GEMINI_MODEL in .env.", 500) from exc
            if exc.code in (401, 403) or "API_KEY" in str(exc):
                raise AgentError(f"Gemini API key was rejected. Check GEMINI_API_KEY in .env. ({detail})", 500) from exc
            raise AgentError(f"Gemini API rejected the request ({exc.code}): {detail}", 400) from exc
        except errors.ServerError as exc:
            raise AgentError(f"Gemini API server error ({exc.code}): {exc.message}. Try again.") from exc
        except errors.APIError as exc:
            raise AgentError(f"Gemini API error ({exc.code}): {exc.message}") from exc
        except OSError as exc:
            raise AgentError(f"Could not reach the Gemini API: {exc}") from exc


def _needs_confirmation(mcp: GoogleAdsMCPClient, call: types.FunctionCall) -> bool:
    """A write tool call that would change something (anything but an explicit dry run)."""
    if not mcp.is_write_tool(call.name):
        return False
    args = call.args or {}
    if mcp.has_param(call.name, "validate_only"):
        # The server defaults validate_only to True, so only an explicit non-True value commits.
        return args.get("validate_only", True) is not True
    return True


def _file_arg_problem(args: dict) -> str | None:
    """Reject server file paths that aren't files attached through the chat."""
    upload_dir = UPLOAD_DIR.resolve()
    for key in FILE_PATH_ARGS:
        value = args.get(key)
        if value in (None, ""):
            continue
        path = Path(str(value)).resolve()
        if path.parent != upload_dir or not path.is_file():
            return (
                f"Error: {key} must be the exact path of a file the user attached in this chat "
                "(shown in their message as [Attached file: ...]). Ask the user to attach the file."
            )
    return None


def _function_declarations(mcp: GoogleAdsMCPClient) -> list[types.FunctionDeclaration]:
    return [
        types.FunctionDeclaration(
            name=tool["name"],
            description=tool["description"],
            parameters_json_schema=tool["input_schema"],
        )
        for tool in mcp.tool_schemas()
    ]


def _response_part(call: types.FunctionCall, text: str, is_error: bool) -> types.Part:
    return types.Part(
        function_response=types.FunctionResponse(
            id=call.id,
            name=call.name,
            response={"error": text} if is_error else {"result": text},
        )
    )


def _trailing_calls(contents: list[types.Content]) -> list[types.FunctionCall]:
    """Function calls in the last turn if it is a model turn still waiting for their results."""
    if not contents or contents[-1].role != "model":
        return []
    return [p.function_call for p in contents[-1].parts or [] if p.function_call]


def _calls_key(calls: list[types.FunctionCall]) -> str:
    return json.dumps([[c.name, c.args or {}] for c in calls], sort_keys=True, default=str)


def _load(history: list[dict]) -> list[types.Content]:
    return [types.Content.model_validate(c) for c in history]


def _append_user_text(contents: list[types.Content], text: str) -> None:
    # If the previous turn ended on tool results (step limit hit, or a change was cancelled),
    # add the new question to that user turn so roles keep alternating.
    if contents and contents[-1].role == "user":
        contents[-1].parts = [*(contents[-1].parts or []), types.Part(text=text)]
    else:
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))


def _text_of(candidate: types.Candidate) -> str:
    parts = candidate.content.parts if candidate.content and candidate.content.parts else []
    return "\n\n".join(p.text for p in parts if p.text and not p.thought).strip()


def _dump(contents: list[types.Content]) -> list[dict]:
    return [c.model_dump(mode="json", exclude_none=True) for c in contents]
