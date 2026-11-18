"""Gmail source adapter.

Read-only. Imports of ``googleapiclient`` are lazy so the core package stays
importable (and testable) without the optional dependency installed. When
credentials are missing the adapter reports itself unavailable with an
actionable reason instead of raising - that becomes a BLOCKED report, not a
crash.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

from .sources import RawMessage

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\b[^>]*>", re.IGNORECASE)
_P_RE = re.compile(r"</p\b[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")

# OAuth client types Google issues. Anything else is not a usable desktop client.
_VALID_CLIENT_KINDS = ("installed", "web")

# Gmail enforces a per-user per-minute unit quota. Reading messages back to back
# trips it, and a tripped quota returns 403, which would otherwise look
# identical to "message could not be parsed". Pace the gets and back off on a
# quota response so a large scan degrades slowly instead of silently losing
# messages.
_GET_INTERVAL_SECONDS = 0.25
_QUOTA_BACKOFF_SECONDS = (5, 15, 45)
_QUOTA_ATTEMPTS = len(_QUOTA_BACKOFF_SECONDS) + 1


def _is_quota_error(exc: Exception) -> bool:
    """True when Google rejected the call for exceeding its rate quota."""

    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    reason = str(getattr(response, "reason", "") or "").lower()
    if status == 429:
        return True
    if status == 403 and ("quota" in reason or "ratelimit" in reason):
        return True
    detail = ""
    content = getattr(exc, "content", None)
    if content:
        try:
            detail = content.decode("utf-8", "replace")[:400].lower()
        except Exception:  # noqa: BLE001 - diagnostics only
            detail = ""
    return status == 403 and ("quota exceeded" in detail or "rate limit" in detail)


class GmailStatus(str, Enum):
    """Explicit outcome of a Gmail read attempt.

    ``OK`` and ``OK_EMPTY`` are the only values that mean Google actually served
    the request. ``CONFIGURED`` means credentials look usable but no read has
    been attempted - it is explicitly *not* a success claim.
    """

    CONFIGURED = "configured"
    OK = "ok"
    OK_EMPTY = "ok_empty"
    MISSING_CONFIG = "missing_config"
    MISSING_CREDENTIALS_FILE = "missing_credentials_file"
    INVALID_CREDENTIALS = "invalid_credentials"
    AUTH_FAILED = "auth_failed"
    DEPENDENCY_MISSING = "dependency_missing"
    API_ERROR = "api_error"


class GmailError(RuntimeError):
    """Raised when Gmail cannot be read. ``status`` is always set."""

    def __init__(self, status: GmailStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class GmailResult:
    """Result of a Gmail read.

    ``messages`` is only non-empty when ``status`` is ``OK``. ``scanned`` is
    True only when the API call actually succeeded - it must never be asserted
    from the mere absence of an exception.

    Field order matters: ``reason`` is second so that ``GmailResult(status,
    reason)`` is the natural way to report a failure.
    """

    status: GmailStatus
    reason: str = ""
    messages: list[RawMessage] = field(default_factory=list)
    message_ids_seen: int = 0
    message_ids_unreadable: int = 0

    @property
    def scanned(self) -> bool:
        return self.status in {GmailStatus.OK, GmailStatus.OK_EMPTY}

    @property
    def read_count(self) -> int:
        return len(self.messages)

    def summary(self) -> str:
        if self.status == GmailStatus.OK:
            return f"Retrieved {len(self.messages)} message(s) from Gmail."
        if self.status == GmailStatus.OK_EMPTY:
            return (
                "Gmail read succeeded but the search matched 0 messages. "
                "No email was read."
            )
        if self.status == GmailStatus.CONFIGURED:
            return (
                "Gmail credentials look usable, but no read has been attempted. "
                "No inbox data was read."
            )
        return f"Gmail read did not succeed ({self.status.value}): {self.reason}"


class GmailSource:
    """Read-only Gmail access via OAuth.

    This adapter never mutates mail. It calls only ``users.messages.list`` and
    ``users.messages.get``, both under the ``gmail.readonly`` scope.
    """

    name = "gmail"

    def __init__(self, config: Any) -> None:
        self._config = config
        self._service: Any | None = None

    @staticmethod
    def validate_client_file(path: Path) -> str | None:
        """Return a human-readable problem string, or ``None`` when the file is OK."""

        try:
            import imaplib
        except ImportError:
            return "client file reading is not supported without the `imaplib` dependency"

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return f"cannot read client file: {exc}"

        if not raw.strip():
            return "client file is empty"

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"client file is not valid JSON ({exc})"

        if not isinstance(payload, dict):
            return "client file JSON is not an object"

        installed = payload.get("installed")
        if not isinstance(installed, dict):
            return "client file JSON has no valid 'installed' object"

        kind = installed.get("kind")
        # Google's installed-client JSON doesn't include "kind", so treat a
        # missing kind as "installed" and only reject genuinely foreign kinds.
        if kind is not None and kind not in _VALID_CLIENT_KINDS:
            return f"unexpected OAuth client kind: {kind!r}"

        client_id = installed.get("client_id")
        client_secret = installed.get("client_secret")
        if not isinstance(client_id, str) or not client_id:
            return "client file is missing 'installed.client_id'"
        if not isinstance(client_secret, str) or not client_secret:
            return "client file is missing 'installed.client_secret'"

        redirect_uris = installed.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return "client file is missing non-empty 'installed.redirect_uris'"
        if not all(isinstance(uri, str) and uri.startswith("http") for uri in redirect_uris):
            return "client file 'installed.redirect_uris' must contain http URLs"

        try:
            imaplib.Debug = 0
        except Exception:
            pass
        return None

    # -- availability ---------------------------------------------------

    def available(self) -> tuple[bool, str]:
        gmail = self._config.gmail
        if gmail.credentials_path is None:
            return (
                False,
                "GMAIL_CREDENTIALS_PATH is not set. Create OAuth desktop credentials "
                "in Google Cloud Console, download the JSON, and set the path.",
            )
        if not gmail.credentials_path.is_file():
            return (False, f"Gmail credentials file not found: {gmail.credentials_path}")
        if gmail.token_path is None:
            return (False, "GMAIL_TOKEN_PATH is not set.")
        return (True, "configured")

    def diagnose(self) -> GmailResult:
        """Check configuration without contacting Google.

        Separates "not configured" from "configured but unusable" so the report
        can say which one it is.
        """

        gmail = self._config.gmail
        if gmail.credentials_path is None:
            return GmailResult(
                GmailStatus.MISSING_CONFIG,
                "GMAIL_CREDENTIALS_PATH is not set. No inbox data was read.",
            )
        if gmail.token_path is None:
            return GmailResult(
                GmailStatus.MISSING_CONFIG,
                "GMAIL_TOKEN_PATH is not set. No inbox data was read.",
            )
        if not gmail.credentials_path.is_file():
            return GmailResult(
                GmailStatus.MISSING_CREDENTIALS_FILE,
                f"Credentials file not found at {gmail.credentials_path}. "
                "No inbox data was read.",
            )

        problem = self.validate_client_file(gmail.credentials_path)
        if problem:
            return GmailResult(
                GmailStatus.INVALID_CREDENTIALS,
                f"{problem} No inbox data was read.",
            )

        if gmail.token_path.is_file():
            try:
                json.loads(gmail.token_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                return GmailResult(
                    GmailStatus.INVALID_CREDENTIALS,
                    f"Cached token at {gmail.token_path} is unreadable ({exc}). "
                    "Delete it and re-run to re-authenticate. No inbox data was read.",
                )

        return GmailResult(
            GmailStatus.CONFIGURED, reason="credentials and token path look usable"
        )

    # -- auth -----------------------------------------------------------

    def _authenticate(self) -> Any:
        if self._service is not None:
            return self._service

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise GmailError(
                GmailStatus.DEPENDENCY_MISSING,
                "Gmail support requires optional dependencies. Install with: "
                "pip install 'hackforge[gmail]'",
            ) from exc

        gmail = self._config.gmail

        problem = self.validate_client_file(gmail.credentials_path)
        if problem:
            raise GmailError(GmailStatus.INVALID_CREDENTIALS, problem)

        credentials = None
        if gmail.token_path.is_file():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(gmail.token_path), _SCOPES
                )
            except (ValueError, json.JSONDecodeError, OSError) as exc:
                raise GmailError(
                    GmailStatus.INVALID_CREDENTIALS,
                    f"Cached token is unusable: {exc}. Delete {gmail.token_path} "
                    "and re-run.",
                ) from exc
            if credentials and credentials.expired and credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    raise GmailError(
                        GmailStatus.AUTH_FAILED,
                        f"Token refresh was rejected by Google: {exc}",
                    ) from exc

        if not credentials or not credentials.valid:
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow

                flow = InstalledAppFlow.from_client_secrets_file(
                    str(gmail.credentials_path), _SCOPES
                )
                credentials = flow.run_local_server(port=0)
            except Exception as exc:  # noqa: BLE001 - OAuth consent/parse failures
                raise GmailError(
                    GmailStatus.AUTH_FAILED,
                    f"OAuth authorisation did not complete: {exc}",
                ) from exc

            gmail.token_path.parent.mkdir(parents=True, exist_ok=True)
            gmail.token_path.write_text(credentials.to_json(), encoding="utf-8")

        try:
            self._service = build("gmail", "v1", credentials=credentials)
        except Exception as exc:  # noqa: BLE001
            raise GmailError(
                GmailStatus.AUTH_FAILED, f"Gmail client could not be constructed: {exc}"
            ) from exc
        return self._service

    # -- reachability (read-only probe, gmail.readonly only) -------------

    def reach(self) -> tuple[bool, str]:
        """Ask Google for a read-only, non-mail piece of profile data.

        Returns (True, masked_address) on success. This is a single, read-only,
        billable-request probe under the same scope as the Gmail reads; it does
        not list, get, modify, send or compose anything.
        """

        service = self._service
        if service is None:
            try:
                service = self._authenticate()
            except GmailError as exc:
                return (False, f"auth error: {exc}")

        try:
            profile = (
                service.users()
                .getProfile(userId="me", fields="emailAddress,messagesTotal")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            return (False, f"api error: {exc}")

        address = profile.get("emailAddress", "")
        if "@" in address:
            local, _, domain = address.partition("@")
            masked = local[:2] + "***@" + domain
        else:
            masked = "***"

        return (True, f"users.getProfile returned 200; account={masked}")

    # -- fetch ----------------------------------------------------------

    def fetch(self, limit: int | None = None) -> list[RawMessage]:
        """Read messages. Raises ``GmailError`` with an explicit status on failure."""

        return self.fetch_detailed(limit).messages

    def fetch_detailed(self, limit: int | None = None) -> GmailResult:
        """Read messages and report exactly what happened.

        The returned ``scanned`` flag is True only when Google actually served
        the request. Configuration problems and auth failures return a non-OK
        status with no messages.
        """

        diagnosis = self.diagnose()
        if diagnosis.status is not GmailStatus.CONFIGURED:
            # MISSING_CONFIG / INVALID_CREDENTIALS / MISSING_CREDENTIALS_FILE
            # all mean no read can be attempted at all.
            return diagnosis

        try:
            service = self._authenticate()
        except GmailError as exc:
            return GmailResult(exc.status, reason=str(exc))

        gmail = self._config.gmail
        max_results = limit or gmail.max_messages

        try:
            listing = (
                service.users()
                .messages()
                .list(userId="me", q=gmail.query, maxResults=max_results)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - API/network failure
            return GmailResult(
                GmailStatus.API_ERROR,
                reason=f"Gmail list call failed: {exc}",
            )

        messages = (listing or {}).get("messages") or []
        if not messages:
            return GmailResult(
                GmailStatus.OK_EMPTY,
                reason=(
                    f"Gmail search '{gmail.query}' matched 0 messages. "
                    "The API call succeeded; no email was read."
                ),
                message_ids_seen=0,
            )

        results: list[RawMessage] = []
        unreadable = 0
        for index, item in enumerate(messages):
            message_id = (item or {}).get("id")
            if not message_id:
                unreadable += 1
                continue
            payload = self._get_with_quota_retry(service, message_id)
            if payload is None:
                unreadable += 1
                continue
            if not isinstance(payload, dict):
                unreadable += 1
                continue
            message = self._normalise(payload)
            if message is not None:
                results.append(message)
            else:
                unreadable += 1
            if index + 1 < len(messages):
                time.sleep(_GET_INTERVAL_SECONDS)

        reason = f"Retrieved {len(results)} of {len(messages)} message(s)."
        if unreadable:
            reason += (
                f" {unreadable} message(s) could not be read from Gmail"
                " (quota or API error) and were skipped."
            )

        return GmailResult(
            GmailStatus.OK,
            messages=results,
            reason=reason,
            message_ids_seen=len(messages),
            message_ids_unreadable=unreadable,
        )

    @staticmethod
    def _get_with_quota_retry(service: Any, message_id: str) -> Any | None:
        """Fetch one message, pacing the read and backing off if quota is hit.

        Returns the raw payload, or ``None`` when the message stays unreadable.
        Never raises: an unreadable message must not abort the whole scan.
        """

        for attempt in range(_QUOTA_ATTEMPTS):
            try:
                return (
                    service.users()
                    .messages()
                    .get(userId="me", id=message_id, format="full")
                    .execute()
                )
            except Exception as exc:  # noqa: BLE001 - skip unreadable message
                if not _is_quota_error(exc) or attempt == _QUOTA_ATTEMPTS - 1:
                    return None
                time.sleep(_QUOTA_BACKOFF_SECONDS[attempt])
        return None

    def _normalise(self, payload: dict[str, Any]) -> RawMessage | None:
        def decode(data: str | None) -> str:
            if not data:
                return ""
            try:
                return base64.urlsafe_b64decode(data).decode("utf-8", "replace")
            except Exception:
                return ""

        raw_payload = payload.get("payload")
        if not isinstance(raw_payload, dict):
            raw_payload = {}

        headers = {}
        for header in raw_payload.get("headers", []) or []:
            if not isinstance(header, dict):
                continue
            name = header.get("name")
            if name:
                headers[name.lower()] = header.get("value", "")

        plain_parts: list[str] = []
        html_parts: list[str] = []
        parts = raw_payload.get("parts", []) or []

        def walk(part: dict[str, Any]) -> None:
            if not isinstance(part, dict):
                return
            mime = part.get("mimeType", "")
            body = part.get("body") or {}
            if not isinstance(body, dict):
                body = {}
            data = body.get("data")
            if mime == "text/plain" and data:
                plain_parts.append(decode(data))
            elif mime == "text/html" and data:
                html_parts.append(decode(data))
            for child in part.get("parts", []) or []:
                walk(child)

        if parts:
            for part in parts:
                walk(part)
        else:
            walk(raw_payload)

        body_text = "\\n".join(plain_parts).strip()
        html_text = "\\n".join(html_parts).strip()
        if not body_text and html_text:
            body_text = self._html_to_text(html_text)

        received = self._parse_date(headers.get("date"))
        sender_raw = headers.get("from", "")
        sender_name, sender_email = self._split_sender(sender_raw)

        return RawMessage(
            message_id=payload.get("id", ""),
            thread_id=payload.get("threadId"),
            subject=headers.get("subject", ""),
            body=body_text,
            sender=sender_email,
            sender_name=sender_name,
            received_at=received,
            source_kind="email",
            source_label=self.name,
            raw_headers=headers,
            html_body=html_text,
        )

    @staticmethod
    def _html_to_text(html: str) -> str:
        stripped = _TAG_RE.sub(" ", html)
        stripped = _BR_RE.sub("\\n", stripped)
        stripped = _P_RE.sub("\\n", stripped)
        stripped = _ANY_TAG_RE.sub(" ", stripped)
        import html as html_module

        stripped = html_module.unescape(stripped)
        return re.sub(r"[ \\t]{2,}", " ", stripped).strip()

    @staticmethod
    def _split_sender(raw: str) -> tuple[str, str]:
        match = re.match(r"^\\s*(.*?)\\s*<([^>]+)>\\s*$", raw or "")
        if match:
            return match.group(1).strip(chr(39) + chr(34)), match.group(2).strip()
        return "", (raw or "").strip()

    @staticmethod
    def _parse_date(raw: str | None) -> date | None:
        if not raw:
            return None
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date()
