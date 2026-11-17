"""Check the Google Ads MCP setup: start the server, list its tools and accessible customers.

Run from the project root:  python scripts/test_mcp.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ConfigError, load_settings  # noqa: E402
from app.mcp_client import GoogleAdsMCPClient, MCPStartupError  # noqa: E402


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\nConfiguration error: {exc}")
        return 1

    print(f"Credentials file: {settings.google_application_credentials}")
    print(f"Google Cloud project: {settings.google_project_id}")
    print(f"Login customer ID: {settings.google_ads_login_customer_id or '(not set)'}")
    print(f"Default customer ID: {settings.google_ads_default_customer_id or '(not set)'}")
    print("Starting google-ads-mcp (the first run can take a minute)...\n")

    mcp = GoogleAdsMCPClient(settings)
    try:
        await mcp.start()
    except MCPStartupError as exc:
        print(f"\nMCP startup failed: {exc}")
        return 1

    try:
        print("\nTools:")
        for tool in mcp.tools:
            summary = (tool.description or "").strip().splitlines()[0:1]
            print(f"  - {tool.name}: {summary[0] if summary else ''}")

        tool_names = {t.name for t in mcp.tools}
        list_tool = next((n for n in tool_names if n.endswith("list_accessible_customers")), None)
        if list_tool is None:
            print("\nNo list_accessible_customers tool found; skipping the credentials check.")
            return 1

        print(f"\nCalling {list_tool}...")
        text, is_error = await mcp.call_tool(list_tool, {})
        print(text)
        if is_error:
            print("\nThe call failed. Re-run scripts/auth.py if the sign-in expired, and check the project's Google Ads API access level (see README).")
            return 1
        print("\nSuccess: the MCP server can reach the Google Ads API.")
        return 0
    finally:
        await mcp.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
