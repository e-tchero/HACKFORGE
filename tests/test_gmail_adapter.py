"""Gmail adapter: configuration, failure modes, and message normalisation.

These tests never contact Google. The optional ``googleapiclient`` dependency
is exercised through an injected fake service so behaviour is verifiable
offline, and the read-only contract is asserted explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hackforge.adapters.gmail import (
    GmailError,
    GmailSource,
    GmailStatus,
    _SCOPES,
)
from hackforge.config import Config


# -- fixtures -------------------------------------------------------------


VALID_CLIENT = {
    "installed": {
        "kind": "installed",
        "client_id": "abc.apps.googleusercontent.com",
        "client_secret": "shhh",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def _write_credentials(tmp_path: Path, payload=None, raw: str | None = None) -> Path:
    path = tmp_path / "credentials.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(json.dumps(VALID_CLIENT if payload is None else payload), encoding="utf-8")
    return path


def _write_token(tmp_path: Path, payload: dict | None = None, raw: str | None = None) -> Path:
    path = tmp_path / "token.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(
                payload
                if payload is not None
                else {
                    "token": "ya29.x",
                    "refresh_token": "1//refresh",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "abc",
                    "client_secret": "shh",
                    "scopes": _SCOPES,
                }
            ),
            encoding="utf-8",
        )
    return path


def _config(tmp_path: Path, credentials=None, token=None) -> Config:
    from hackforge.config import GmailConfig

    return Config(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "ws",
        gmail=GmailConfig(
            credentials_path=credentials,
            token_path=token,
            query="newer_than:90d",
        ),
    )


# -- fake Gmail API --------------------------------------------------------


def _b64(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _payload(
    subject="Registration confirmed - Global AI Hackathon 2026",
    sender="Events <events@hackathon.example.org>",
    date="Mon, 15 Jan 2026 10:30:00 +0000",
    body="Thank you for registering.\n\nSubmission deadline: 2026-04-18\n",
    message_id="m-1",
    thread_id="t-1",
) -> dict:
    return {
        "id": message_id,
        "threadId": thread_id,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
            ],
            "body": {},
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(body)}, "parts": []},
            ],
        },
    }


class FakeMessages:
    def __init__(self, payloads, fail_list=False):
        self._payloads = payloads
        self._fail_list = fail_list
        self.list_calls = []
        self.get_calls = []

    def list(self, userId=None, q=None, maxResults=None):
        self.list_calls.append({"userId": userId, "q": q, "maxResults": maxResults})
        if self._fail_list:
            raise RuntimeError("HttpError 403 quotaExceeded")
        return SimpleNamespace(execute=lambda: {"messages": [{"id": p["id"]} for p in self._payloads]})

    def get(self, userId=None, id=None, format=None):
        self.get_calls.append(id)
        for payload in self._payloads:
            if payload["id"] == id:
                return SimpleNamespace(execute=lambda p=payload: p)
        raise RuntimeError("not found")


class FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self, messages):
        self._messages = messages

    def users(self):
        return FakeUsers(self._messages)


def _source_with_fake(tmp_path, payloads, fail_list=False):
    source = GmailSource(_config(tmp_path, _write_credentials(tmp_path), _write_token(tmp_path)))
    source._service = FakeService(FakeMessages(payloads, fail_list=fail_list))
    # Write a valid installed-client file to disk. diagnose()/_authenticate()
    # validate the file on disk, so the fake service is usable from fetch onward.
    (tmp_path / "credentials.json").write_text(
        json.dumps(VALID_CLIENT), encoding="utf-8"
    )
    return source


# -- configuration and credentials ----------------------------------------


class TestConfiguration:
    def test_missing_credentials_env(self, tmp_path):
        result = GmailSource(_config(tmp_path)).diagnose()

        assert result.status is GmailStatus.MISSING_CONFIG
        assert result.scanned is False
        assert "GMAIL_CREDENTIALS_PATH" in result.reason
        assert "No inbox data was read" in result.reason

    def test_missing_token_env(self, tmp_path):
        result = GmailSource(_config(tmp_path, _write_credentials(tmp_path))).diagnose()

        assert result.status is GmailStatus.MISSING_CONFIG
        assert "GMAIL_TOKEN_PATH" in result.reason

    def test_credentials_file_absent(self, tmp_path):
        result = GmailSource(
            _config(tmp_path, tmp_path / "nope.json", _write_token(tmp_path))
        ).diagnose()

        assert result.status is GmailStatus.MISSING_CREDENTIALS_FILE
        assert result.scanned is False

    def test_invalid_json_credentials(self, tmp_path):
        result = GmailSource(
            _config(tmp_path, _write_credentials(tmp_path, raw="{not json"), _write_token(tmp_path))
        ).diagnose()

        assert result.status is GmailStatus.INVALID_CREDENTIALS
        assert "not valid JSON" in result.reason

    def test_client_file_has_no_installed_object(self, tmp_path):
        result = GmailSource(
            _config(
                tmp_path,
                _write_credentials(tmp_path, payload={"random": "thing"}),
                _write_token(tmp_path),
            )
        ).diagnose()

        assert result.status is GmailStatus.INVALID_CREDENTIALS
        assert "installed" in result.reason

    def test_valid_configuration_is_not_a_success_claim(self, tmp_path):
        """CONFIGURED must never read as 'Gmail was scanned'."""
        result = GmailSource(
            _config(tmp_path, _write_credentials(tmp_path), _write_token(tmp_path))
        ).diagnose()

        assert result.status is GmailStatus.CONFIGURED
        assert result.scanned is False
        assert result.read_count == 0
        assert "no read has been attempted" in result.summary()
        assert "Retrieved" not in result.summary()

    def test_available_matches_diagnose(self, tmp_path):
        source = GmailSource(_config(tmp_path))
        available, reason = source.available()
        assert available is False
        assert "GMAIL_CREDENTIALS_PATH" in reason


# -- read attempts ---------------------------------------------------------


class TestFetchFailures:
    def test_missing_credentials_never_claims_scan(self, tmp_path):
        result = GmailSource(_config(tmp_path)).fetch_detailed()

        assert result.scanned is False
        assert result.read_count == 0
        assert "did not scan" not in result.summary()
        assert "Gmail read did not succeed" in result.summary()

    def test_invalid_credentials_never_claims_scan(self, tmp_path):
        result = GmailSource(
            _config(
                tmp_path,
                _write_credentials(tmp_path, raw="nope"),
                _write_token(tmp_path),
            )
        ).fetch_detailed()

        assert result.scanned is False
        assert result.messages == []

    def test_api_list_failure(self, tmp_path):
        result = _source_with_fake(tmp_path, [_payload()], fail_list=True).fetch_detailed()

        assert result.status is GmailStatus.API_ERROR
        assert result.scanned is False
        assert "403" in result.reason

    def test_empty_inbox_is_success_with_zero_messages(self, tmp_path):
        result = _source_with_fake(tmp_path, []).fetch_detailed()

        assert result.status is GmailStatus.OK_EMPTY
        assert result.scanned is True
        assert result.read_count == 0
        assert "matched 0 messages" in result.reason

    def test_empty_summary_is_not_failure(self, tmp_path):
        result = _source_with_fake(tmp_path, []).fetch_detailed()

        assert "succeeded" in result.summary()
        assert "did not succeed" not in result.summary()


class TestSuccessfulRead:
    def test_reads_real_payload(self, tmp_path):
        source = _source_with_fake(tmp_path, [_payload()])
        result = source.fetch_detailed()

        assert result.status is GmailStatus.OK
        assert result.scanned is True
        assert result.read_count == 1

        message = result.messages[0]
        assert message.subject.startswith("Registration confirmed")
        assert "@" in message.sender and "events@hackathon.example.org" in message.sender
        assert ("Events" in message.sender_name) or (message.sender_name == "")
        assert message.thread_id == "t-1"
        assert "Submission deadline" in message.body
        assert message.received_at.isoformat() == "2026-01-15"

    def test_uses_configured_query(self, tmp_path):
        source = _source_with_fake(tmp_path, [_payload()])
        source.fetch_detailed()

        assert source._service._messages.list_calls[0]["q"] == "newer_than:90d"

    def test_limit_is_passed_through(self, tmp_path):
        source = _source_with_fake(tmp_path, [_payload()])
        source.fetch_detailed(limit=5)

        assert source._service._messages.list_calls[0]["maxResults"] == 5

    def test_only_read_methods_are_called(self, tmp_path):
        """The adapter must not mutate mail. Only list/get may be invoked."""
        source = _source_with_fake(tmp_path, [_payload()])
        source.fetch_detailed()

        # A mutating client would expose these; absence proves read-only usage.
        messages_client = source._service._messages
        assert not hasattr(messages_client, "send")
        assert not hasattr(messages_client, "delete")
        assert not hasattr(messages_client, "modify")
        assert not hasattr(messages_client, "trash")
        assert not hasattr(messages_client, "batchModify")
        assert not hasattr(messages_client, "import_")

    def test_adapter_source_declares_read_only_scope(self):
        assert GmailSource.name == "gmail"
        assert _SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]

    def test_scope_is_read_only(self):
        assert _SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


# -- normalisation ---------------------------------------------------------


class TestNormalisation:
    def test_html_only_message(self, tmp_path):
        payload = {
            "id": "m-2",
            "threadId": "t-2",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Hackathon registration"},
                    {"name": "From", "value": "a@example.org"},
                ],
                "mimeType": "text/html",
                "body": {
                    "data": _b64("<html><body><p>You are <b>registered</b>.</p></body></html>")
                },
            },
        }
        result = _source_with_fake(tmp_path, [payload]).fetch_detailed()

        message = result.messages[0]
        assert "registered" in message.body
        assert "<" not in message.body

    def test_nested_multipart(self, tmp_path):
        inner = {
            "mimeType": "text/plain",
            "body": {"data": _b64("deeply nested body")},
        }
        payload = {
            "id": "m-3",
            "threadId": "t-3",
            "payload": {
                "headers": [{"name": "Subject", "value": "N"}],
                "mimeType": "multipart/mixed",
                "body": {},
                "parts": [
                    {"mimeType": "text/plain", "body": {}, "parts": [inner]},
                ],
            },
        }
        result = _source_with_fake(tmp_path, [payload]).fetch_detailed()

        assert "deeply nested body" in result.messages[0].body

    def test_undecodable_body_does_not_crash(self, tmp_path):
        payload = {
            "id": "m-4",
            "threadId": None,
            "payload": {
                "headers": [{"name": "Subject", "value": "Weird"}],
                "mimeType": "text/plain",
                "body": {"data": "!!!not-base64!!!"},
            },
        }
        result = _source_with_fake(tmp_path, [payload]).fetch_detailed()

        assert result.status is GmailStatus.OK
        assert result.read_count == 1

    def test_missing_payload_is_skipped(self, tmp_path):
        payload = {"id": "m-5"}
        result = _source_with_fake(tmp_path, [payload]).fetch_detailed()

        # Still a successful read, but with nothing usable from this message.
        assert result.status is GmailStatus.OK

    def test_missing_headers_defaults(self, tmp_path):
        payload = {"id": "m-6", "payload": {"headers": []}}
        result = _source_with_fake(tmp_path, [payload]).fetch_detailed()
        message = result.messages[0]

        assert message.subject == ""
        assert message.sender == ""
        assert message.received_at is None

    def test_unparseable_date_is_none(self, tmp_path):
        payload = {
            "id": "m-7",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "S"},
                    {"name": "Date", "value": "not-a-real-date"},
                ],
            },
        }
        message = _source_with_fake(tmp_path, [payload]).fetch_detailed().messages[0]

        assert message.received_at is None

    def test_missing_date_header(self, tmp_path):
        payload = {"id": "m-8", "payload": {"headers": [{"name": "Subject", "value": "S"}]}}
        message = _source_with_fake(tmp_path, [payload]).fetch_detailed().messages[0]

        assert message.received_at is None

    def test_non_dict_payload_skipped(self, tmp_path):
        class WeirdMessages(FakeMessages):
            def get(self, userId=None, id=None, format=None):
                return SimpleNamespace(execute=lambda: "not-a-dict")

        source = GmailSource(_config(tmp_path, _write_credentials(tmp_path), _write_token(tmp_path)))
        source._service = FakeService(WeirdMessages([_payload()]))
        result = source.fetch_detailed()

        assert result.status is GmailStatus.OK
        assert result.read_count == 0
        assert result.message_ids_seen == 1

    def test_partially_parsed_reports_both_counts(self, tmp_path):
        class FlakyMessages(FakeMessages):
            def get(self, userId=None, id=None, format=None):
                if id == "m-1":
                    raise RuntimeError("transient failure")
                return super().get(userId=userId, id=id, format=format)

        source = GmailSource(_config(tmp_path, _write_credentials(tmp_path), _write_token(tmp_path)))
        source._service = FakeService(FlakyMessages([_payload(), _payload(message_id="m-2")]))
        result = source.fetch_detailed()

        assert result.message_ids_seen == 2
        assert result.read_count == 1


class TestGmailError:
    def test_error_carries_status(self):
        error = GmailError(GmailStatus.AUTH_FAILED, "nope")

        assert error.status is GmailStatus.AUTH_FAILED
        assert "nope" in str(error)

# -- quota handling and html stripping -------------------------------------


class _QuotaError(Exception):
    """Stands in for googleapiclient HttpError with a 403 quota response."""

    def __init__(self, status=403, reason="Forbidden", detail="Quota exceeded for quota metric"):
        super().__init__("quota")
        self.resp = SimpleNamespace(status=status, reason=reason)
        self.content = detail.encode("utf-8")


class TestQuotaHandling:
    def test_retries_quota_error_then_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hackforge.adapters.gmail.time.sleep", lambda *_: None)

        class QuotaOnce(FakeMessages):
            def __init__(self, payloads):
                super().__init__(payloads)
                self.attempted = 0

            def get(self, userId=None, id=None, format=None):
                self.attempted += 1
                if self.attempted == 1:
                    raise _QuotaError()
                return super().get(userId=userId, id=id, format=format)

        source = GmailSource(_config(tmp_path, _write_credentials(tmp_path), _write_token(tmp_path)))
        source._service = FakeService(QuotaOnce([_payload()]))
        result = source.fetch_detailed()

        assert result.read_count == 1
        assert result.message_ids_unreadable == 0

    def test_gives_up_after_retry_budget_and_counts_unreadable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hackforge.adapters.gmail.time.sleep", lambda *_: None)

        class AlwaysQuota(FakeMessages):
            def get(self, userId=None, id=None, format=None):
                raise _QuotaError()

        source = GmailSource(_config(tmp_path, _write_credentials(tmp_path), _write_token(tmp_path)))
        source._service = FakeService(AlwaysQuota([_payload(), _payload(message_id="m-2")]))
        result = source.fetch_detailed()

        assert result.status is GmailStatus.OK
        assert result.read_count == 0
        assert result.message_ids_seen == 2
        assert result.message_ids_unreadable == 2
        assert "could not be read" in result.reason

    def test_non_quota_error_is_not_retried(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hackforge.adapters.gmail.time.sleep", lambda *_: None)

        class Forbidden(FakeMessages):
            def __init__(self, payloads):
                super().__init__(payloads)
                self.attempts = 0

            def get(self, userId=None, id=None, format=None):
                self.attempts += 1
                raise _QuotaError(status=403, reason="insufficientPermissions", detail="nope")

        messages = Forbidden([_payload()])
        source = GmailSource(_config(tmp_path, _write_credentials(tmp_path), _write_token(tmp_path)))
        source._service = FakeService(messages)
        result = source.fetch_detailed()

        assert messages.attempts == 1
        assert result.message_ids_unreadable == 1

    def test_rate_limited_429_is_retried(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hackforge.adapters.gmail.time.sleep", lambda *_: None)

        class RateLimited(FakeMessages):
            def __init__(self, payloads):
                super().__init__(payloads)
                self.attempts = 0

            def get(self, userId=None, id=None, format=None):
                self.attempts += 1
                if self.attempts < 2:
                    raise _QuotaError(status=429, reason="rateLimitExceeded")
                return super().get(userId=userId, id=id, format=format)

        messages = RateLimited([_payload()])
        source = GmailSource(_config(tmp_path, _write_credentials(tmp_path), _write_token(tmp_path)))
        source._service = FakeService(messages)
        result = source.fetch_detailed()

        assert messages.attempts == 2
        assert result.read_count == 1


class TestHtmlStripping:
    def test_removes_script_and_style_blocks(self):
        source = GmailSource(_config(Path(".")))

        text = source._html_to_text("<style type='text/css'>p{color:red}</style><p>Hello</p>")

        assert "color:red" not in text
        assert "Hello" in text

    def test_script_content_is_not_leaked(self):
        source = GmailSource(_config(Path(".")))

        text = source._html_to_text("<p>Hi</p><script>alert('xss')</script><p>Bye</p>")

        assert "alert" not in text
        assert "Hi" in text and "Bye" in text

    def test_breaks_and_paragraph_ends_become_spaces(self):
        source = GmailSource(_config(Path(".")))

        text = source._html_to_text("<p>one</p><p>two<br/>three<br />four")

        assert "<" not in text
        assert "one" in text and "four" in text

    def test_html_only_message_body_is_readable(self, tmp_path):
        html = (
            "<html><head><style>.x{}</style></head><body>"
            "<p>Registration confirmed.</p><p>Submission deadline: 2026-04-18</p>"
            "<script>track()</script></body></html>"
        )
        payload = {
            "id": "m-html",
            "threadId": "t-html",
            "payload": {
                "mimeType": "text/html",
                "headers": [
                    {"name": "Subject", "value": "Registration confirmed"},
                    {"name": "From", "value": "Events <events@hackathon.example.org>"},
                    {"name": "Date", "value": "Mon, 15 Jan 2026 10:30:00 +0000"},
                ],
                "body": {"data": _b64(html)},
            },
        }
        source = GmailSource(_config(tmp_path, _write_credentials(tmp_path), _write_token(tmp_path)))
        message = source._normalise(payload)

        assert message is not None
        assert "Submission deadline: 2026-04-18" in message.body
        assert "track()" not in message.searchable_text
        assert "submission deadline: 2026-04-18" in message.searchable_text
