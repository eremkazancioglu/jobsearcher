"""
One-time interactive script to generate a Gmail OAuth token.

Run once locally: uv run scripts/gmail_oauth_setup.py
Opens a browser for consent, then writes personal/gmail_token.json (the
refresh token) next to personal/gmail_credentials.json. Both files are
gitignored under personal/, same as the resume/preferences documents.

Not needed again unless the token is revoked or the requested scope
changes -- agents/digest_source.py and agents/tracker.py read the saved
token directly, not this script.
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_PATH = "personal/gmail_credentials.json"
TOKEN_PATH = "personal/gmail_token.json"


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    print(f"Token saved to {TOKEN_PATH}")


if __name__ == "__main__":
    main()
