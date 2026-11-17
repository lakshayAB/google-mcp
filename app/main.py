"""FastAPI app: starts the shared MCP session on startup and serves the chat API + UI."""

import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from app.agent import AgentError, ChatResult, GeminiAgent
from app.config import PROJECT_ROOT, UPLOAD_DIR, ConfigError, load_settings
from app.mcp_client import GoogleAdsMCPClient, MCPStartupError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app")

INDEX_HTML = PROJECT_ROOT / "static" / "index.html"

# Attachments for image assets and video uploads. Google Ads image assets are at most 5 MB.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".wmv", ".flv", ".mpeg", ".mpg", ".webm", ".mkv", ".3gp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        raise

    mcp = GoogleAdsMCPClient(settings)
    try:
        await mcp.start()
    except MCPStartupError as exc:
        logger.error("%s", exc)
        raise

    app.state.mcp = mcp
    app.state.agent = GeminiAgent(settings)
    write_tools = [t.name for t in mcp.tools if mcp.is_write_tool(t.name)]
    logger.info("Ready. Using model %s. Write tools (need confirmation): %s", settings.gemini_model, write_tools or "none")
    try:
        yield
    finally:
        await mcp.stop()


app = FastAPI(title="Google Ads Chatbot PoC", lifespan=lifespan)


class Confirmation(BaseModel):
    action_id: str = Field(min_length=1)
    approve: bool


class ChatRequest(BaseModel):
    message: str | None = None
    # Answer to a pending change instead of a new message.
    confirm: Confirmation | None = None
    # Gemini contents, opaque to the browser.
    history: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_of(self):
        has_message = bool(self.message and self.message.strip())
        if has_message == (self.confirm is not None):
            raise ValueError("Send either a message or a confirmation.")
        return self


class ChatResponse(BaseModel):
    answer: str
    history: list[dict]
    pending_action: dict | None = None


class UploadResponse(BaseModel):
    path: str
    name: str
    kind: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


@app.get("/health")
async def health() -> dict:
    mcp = app.state.mcp
    return {
        "status": "ok",
        "tools": [t.name for t in mcp.tools],
        "write_tools": [t.name for t in mcp.tools if mcp.is_write_tool(t.name)],
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    for m in request.history:
        if m.get("role") not in ("user", "model"):
            raise HTTPException(400, f"Invalid role in history: {m.get('role')!r}. Start a new chat.")

    agent: GeminiAgent = app.state.agent
    try:
        if request.confirm is not None:
            result: ChatResult = await agent.resume(
                request.confirm.action_id, request.confirm.approve, request.history, app.state.mcp
            )
        else:
            result = await agent.chat(request.message.strip(), request.history, app.state.mcp)
    except AgentError as exc:
        logger.error("Chat failed: %s", exc)
        raise HTTPException(exc.status_code, str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error while answering")
        raise HTTPException(500, f"Unexpected server error: {type(exc).__name__}") from exc
    return ChatResponse(answer=result.answer, history=result.history, pending_action=result.pending_action)


@app.post("/upload", response_model=UploadResponse)
async def upload(request: Request, filename: str = Query(min_length=1, max_length=200)) -> UploadResponse:
    """Save a file sent as the raw request body, for image assets or video uploads."""
    original = Path(filename).name
    ext = Path(original).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        kind, limit = "image", MAX_IMAGE_BYTES
    elif ext in VIDEO_EXTENSIONS:
        kind, limit = "video", MAX_VIDEO_BYTES
    else:
        raise HTTPException(400, "Only images (png, jpg, gif) and videos (mp4, mov, webm, ...) can be attached.")

    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(original).stem)[:60] or "file"
    UPLOAD_DIR.mkdir(exist_ok=True)
    target = UPLOAD_DIR / f"{uuid.uuid4().hex[:12]}_{stem}{ext}"
    size = 0
    try:
        with target.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"File too large: {kind}s can be at most {limit // (1024 * 1024)} MB.")
                out.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "The file is empty.")

    logger.info("Saved upload %s (%d bytes)", target.name, size)
    return UploadResponse(path=str(target.resolve()), name=original, kind=kind)
