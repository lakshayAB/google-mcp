"""Load and validate configuration from environment variables (.env)."""

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Pinned: the write (propose-and-confirm) handling in app/agent.py depends on this version's tools.
GOOGLE_ADS_MCP_SPEC = "git+https://github.com/googleads/google-ads-mcp.git@v0.0.6"
DEFAULT_CREDENTIALS_PATH = PROJECT_ROOT / "credentials" / "credentials.json"
# Turns the MCP server's tool groups on or off (set labels/assets to false for read-only).
DEFAULT_TOOLS_CONFIG_PATH = PROJECT_ROOT / "tools_config.yaml"
# Files attached in the chat. The only files image and video tools may read.
UPLOAD_DIR = PROJECT_ROOT / "uploads"

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    google_project_id: str
    google_application_credentials: Path
    google_ads_login_customer_id: str | None
    google_ads_default_customer_id: str | None
    mcp_command: str
    mcp_args: list[str]
    tools_config: Path

    def mcp_env(self) -> dict[str, str]:
        """Environment variables passed to the google-ads-mcp subprocess."""
        env = {
            "GOOGLE_PROJECT_ID": self.google_project_id,
            "GOOGLE_APPLICATION_CREDENTIALS": str(self.google_application_credentials),
            "GOOGLE_ADS_MCP_TOOLS_CONFIG": str(self.tools_config),
        }
        if self.google_ads_login_customer_id:
            env["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = self.google_ads_login_customer_id
        return env


def _get(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _customer_id(name: str) -> str | None:
    value = _get(name)
    if value is None:
        return None
    digits = value.replace("-", "")
    if not digits.isdigit() or len(digits) != 10:
        raise ConfigError(f"{name} must be a 10-digit Google Ads customer ID (got {value!r}).")
    return digits


def _mcp_launch_command() -> tuple[str, list[str]]:
    """Work out how to start google-ads-mcp: explicit override, then pipx, then uvx."""
    override = _get("GOOGLE_ADS_MCP_COMMAND")
    if override:
        parts = shlex.split(override, posix=os.name != "nt")
        return parts[0], parts[1:]
    if shutil.which("pipx"):
        return "pipx", ["run", "--spec", GOOGLE_ADS_MCP_SPEC, "google-ads-mcp"]
    if shutil.which("uvx"):
        return "uvx", ["--from", GOOGLE_ADS_MCP_SPEC, "google-ads-mcp"]
    raise ConfigError(
        "Could not find pipx or uvx on PATH to launch google-ads-mcp. Install one of them "
        "(see README) or set GOOGLE_ADS_MCP_COMMAND to the full command that starts the server."
    )


def credentials_path() -> Path:
    """The ADC file passed to the MCP server (GOOGLE_APPLICATION_CREDENTIALS, resolved)."""
    path = Path(_get("GOOGLE_APPLICATION_CREDENTIALS") or DEFAULT_CREDENTIALS_PATH)
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def _existing_credentials() -> Path:
    path = credentials_path()
    if not path.is_file():
        raise ConfigError(
            f"No Google credentials found at {path}. Run `python scripts/auth.py` once to sign "
            "in and create it (or point GOOGLE_APPLICATION_CREDENTIALS at an existing ADC file)."
        )
    return path


def _tools_config() -> Path:
    path = Path(_get("GOOGLE_ADS_MCP_TOOLS_CONFIG") or DEFAULT_TOOLS_CONFIG_PATH)
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    if not path.is_file():
        raise ConfigError(f"MCP tools config not found at {path} (set GOOGLE_ADS_MCP_TOOLS_CONFIG).")
    return path


def load_settings() -> Settings:
    missing = [name for name in ("GEMINI_API_KEY", "GOOGLE_PROJECT_ID") if _get(name) is None]
    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    credentials = _existing_credentials()
    command, args = _mcp_launch_command()

    return Settings(
        gemini_api_key=_get("GEMINI_API_KEY"),
        gemini_model=_get("GEMINI_MODEL") or "gemini-3.8-flash",
        google_project_id=_get("GOOGLE_PROJECT_ID"),
        google_application_credentials=credentials,
        google_ads_login_customer_id=_customer_id("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
        google_ads_default_customer_id=_customer_id("GOOGLE_ADS_DEFAULT_CUSTOMER_ID"),
        mcp_command=command,
        mcp_args=args,
        tools_config=_tools_config(),
    )
