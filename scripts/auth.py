"""One-time Google sign-in: saves an ADC "authorized_user" file for the Google Ads MCP server.

Uses GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET from .env (a Desktop app OAuth
client) and writes the file to GOOGLE_APPLICATION_CREDENTIALS
(default: credentials/credentials.json).

Run from the project root:  python scripts/auth.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from app.config import credentials_path  # noqa: E402 (also loads .env)

SCOPES = ["https://www.googleapis.com/auth/adwords"]


def main() -> int:
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env first (Desktop app OAuth client).")
        return 1

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)

    print("Opening your browser to sign in. Use the Google account that can access your Google Ads accounts.")
    # offline + consent so Google always returns a refresh token.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not credentials.refresh_token:
        print("Google did not return a refresh token. Remove this app's access at "
              "https://myaccount.google.com/permissions and run this script again.")
        return 1

    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": credentials.refresh_token,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nSuccess: credentials saved to {path}")
    print("Next: python scripts/test_mcp.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
