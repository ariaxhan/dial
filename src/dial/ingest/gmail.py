"""Reading a real mailbox, read-only, with the smallest possible blast radius.

This is a thin fetch layer. Everything that decides what a message *means* lives in
`dial.ingest.mailbox`, which is why that module is heavily tested and this one is not: this
one has no judgement in it, it just turns the Gmail API into `Message` objects.

## The thing worth knowing before you try this

Gmail read scopes are **Restricted**. Publishing an app that uses them requires a CASA
security assessment, and that review takes longer than this hackathon lasts. So the OAuth
client stays in **Testing** mode, which allows up to 100 named test users with no review at
all. That is a deliberate, stated limitation rather than something for a judge to discover.

## Setup

1. Create a Google Cloud project and enable the Gmail API.
2. Create an OAuth client of type "Desktop app" and download the JSON.
3. Put it at `~/.config/dial/google-credentials.json`, or point `DIAL_GOOGLE_CREDENTIALS`
   at it.
4. On the consent screen, keep publishing status "Testing" and add yourself as a test user.

The token cache lands in `~/.config/dial/gmail-token.json` and nothing is written anywhere
else. No credential ever enters this repository.

## Scope

`gmail.readonly` and nothing more. Dial never sends, never deletes, never modifies a label.
The agent that can place phone calls has no ability to touch your mail.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import re
from pathlib import Path

from dial.ingest.mailbox import Message

__all__ = ["SCOPES", "GmailConnector", "default_query"]

#: Read only. Deliberately the narrowest scope that can do the job.
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)

CONFIG_DIR = Path(os.environ.get("DIAL_CONFIG_DIR", Path.home() / ".config" / "dial"))
CREDENTIALS_PATH = Path(
    os.environ.get("DIAL_GOOGLE_CREDENTIALS", CONFIG_DIR / "google-credentials.json")
)
TOKEN_PATH = Path(os.environ.get("DIAL_GMAIL_TOKEN", CONFIG_DIR / "gmail-token.json"))

#: The mail worth reading. Narrowing at the API rather than downloading a whole mailbox
#: keeps this fast and means far less of someone's private mail is ever in memory.
_QUERY_TERMS = (
    "receipt", "invoice", '"your payment"', '"payment received"', '"free trial"',
    '"has been charged"', '"auto-renew"', '"will renew"', '"price change"',
    '"your return"', "refund", '"check-in"', '"continue watching"', '"shared with you"',
)


def default_query(months: int = 12) -> str:
    """A Gmail search string covering the mail Dial can actually use."""
    return f"newer_than:{months}m AND ({' OR '.join(_QUERY_TERMS)})"


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _plain_text(payload: dict) -> str:
    """Depth-first walk for the text/plain part, falling back to stripped HTML."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})

    if mime == "text/plain" and body.get("data"):
        return _decode(body["data"])

    for part in payload.get("parts", []) or []:
        found = _plain_text(part)
        if found:
            return found

    if mime == "text/html" and body.get("data"):
        return re.sub(r"<[^>]+>", " ", _decode(body["data"]))
    return ""


def _header(headers: list[dict], name: str) -> str:
    lowered = name.lower()
    for header in headers:
        if header.get("name", "").lower() == lowered:
            return header.get("value", "")
    return ""


class GmailConnector:
    """Fetches messages. Holds no judgement about what they mean."""

    def __init__(self, service: object | None = None) -> None:
        self._service = service

    def authorize(self) -> None:
        """Run the installed-app OAuth flow, caching the token under the config dir."""
        from google.auth.transport.requests import Request           # type: ignore
        from google.oauth2.credentials import Credentials            # type: ignore
        from google_auth_oauthlib.flow import InstalledAppFlow       # type: ignore
        from googleapiclient.discovery import build                  # type: ignore

        creds = None
        if TOKEN_PATH.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), list(SCOPES))

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not CREDENTIALS_PATH.exists():
                    raise FileNotFoundError(
                        f"No Google OAuth client at {CREDENTIALS_PATH}. "
                        "See the setup steps in this module's docstring."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_PATH), list(SCOPES)
                )
                creds = flow.run_local_server(port=0)
            TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_PATH.write_text(creds.to_json())
            TOKEN_PATH.chmod(0o600)

        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def fetch(self, query: str | None = None, *, limit: int = 400) -> list[Message]:
        """Fetch messages matching `query` and reduce them to `Message` objects."""
        if self._service is None:
            self.authorize()
        assert self._service is not None
        service = self._service

        query = query or default_query()
        messages: list[Message] = []
        page_token: str | None = None

        while len(messages) < limit:
            listing = (
                service.users()  # type: ignore[attr-defined]
                .messages()
                .list(
                    userId="me",
                    q=query,
                    pageToken=page_token,
                    maxResults=min(100, limit - len(messages)),
                )
                .execute()
            )
            ids = [m["id"] for m in listing.get("messages", [])]
            for message_id in ids:
                raw = (
                    service.users()  # type: ignore[attr-defined]
                    .messages()
                    .get(userId="me", id=message_id, format="full")
                    .execute()
                )
                parsed = self._to_message(raw)
                if parsed is not None:
                    messages.append(parsed)

            page_token = listing.get("nextPageToken")
            if not page_token or not ids:
                break

        return messages

    @staticmethod
    def _to_message(raw: dict) -> Message | None:
        payload = raw.get("payload", {}) or {}
        headers = payload.get("headers", []) or []

        # internalDate is epoch milliseconds and is far more reliable than the Date header,
        # which senders routinely get wrong.
        try:
            when = dt.datetime.fromtimestamp(
                int(raw["internalDate"]) / 1000, tz=dt.timezone.utc
            ).date()
        except (KeyError, ValueError, TypeError):
            return None

        sender = _header(headers, "From")
        if not sender:
            return None

        return Message(
            id=raw.get("id", ""),
            sender=sender,
            subject=_header(headers, "Subject"),
            date=when,
            body=_plain_text(payload)[:20_000],
        )
