"""Shared read-only Gmail access. Built for agents/digest_source.py
(Phase 4) but not specific to it -- agents/tracker.py (Phase 3, whenever
that's picked up) reads the same token, same scope.

One-time setup: scripts/gmail_oauth_setup.py, which writes
personal/gmail_token.json (gitignored under personal/, same as the
resume/preferences documents). This module only ever reads that token and
refreshes it in place -- it doesn't run the interactive consent flow.
"""

import base64
import logging
import os
import time
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", "personal/gmail_token.json")

# Confirmed in practice: fetching several messages in a tight loop via
# messages.get can hit Gmail API's per-minute-per-user rate limit (a 403
# with reason "rateLimitExceeded"), same transient-failure category as
# Adzuna's retried 5xx in mcp_servers/job_sources/server.py. Retried here
# the same way -- a handful of digest emails per run shouldn't need more
# than a couple of backoff cycles to clear.
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
RETRY_BACKOFF_S = 5.0


def _execute_with_retry(request):
    attempt = 1
    while True:
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status
            if status not in RETRYABLE_STATUS_CODES or attempt >= MAX_ATTEMPTS:
                raise
            wait_s = RETRY_BACKOFF_S * attempt
            logger.warning(
                "Gmail API call failed (status %d, attempt %d/%d) -- retrying in %.0fs",
                status, attempt, MAX_ATTEMPTS, wait_s,
            )
            time.sleep(wait_s)
            attempt += 1


def _get_credentials() -> Credentials:
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        Path(TOKEN_PATH).write_text(creds.to_json())
    return creds


def get_service():
    return build("gmail", "v1", credentials=_get_credentials())


def _extract_html(payload: dict) -> Optional[str]:
    """Walk a Gmail message payload's MIME parts for the text/html body --
    digest emails are multipart/alternative (plain text + html), same
    shape confirmed against real LinkedIn/BuiltIn/Wellfound samples."""
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        html = _extract_html(part)
        if html:
            return html
    return None


def search_html_messages(sender_query: str, after_epoch_s: int) -> list[str]:
    """Returns the HTML body of every message matching sender_query
    received after after_epoch_s (Gmail search's `after:` filter takes
    epoch seconds). Messages with no text/html part are skipped, not
    raised on -- a genuinely empty/malformed digest email is a normal
    degrade case, not a crash."""
    service = get_service()
    gmail_query = f"{sender_query} after:{after_epoch_s}"

    message_ids = []
    page_token = None
    while True:
        response = _execute_with_retry(
            service.users().messages().list(userId="me", q=gmail_query, pageToken=page_token)
        )
        message_ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    bodies = []
    for msg_id in message_ids:
        message = _execute_with_retry(
            service.users().messages().get(userId="me", id=msg_id, format="full")
        )
        html = _extract_html(message["payload"])
        if html:
            bodies.append(html)
        else:
            logger.warning("Message %s matching %r had no HTML body -- skipping", msg_id, sender_query)
    return bodies
