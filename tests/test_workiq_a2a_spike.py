import json

import pytest

from scripts import spike_workiq_a2a as spike


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def schedule():
    return {
        "attendee": {
            "name": "Rima Reyes",
            "email": "rima.reyes@microsoft.com",
            "role": "Principal Product Manager",
        },
        "alternatives": [],
        "slots": [
            {
                "start": "2026-08-17T10:00:00",
                "end": "2026-08-17T10:30:00",
                "timeZone": "Eastern Standard Time",
            }
        ],
    }


def completed_response(artifact=None):
    return {
        "jsonrpc": "2.0",
        "id": "request-id",
        "result": {
            "task": {
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {"parts": [{"text": json.dumps(artifact or schedule())}]}
                ],
            }
        },
    }


def test_build_request_uses_a2a_v1_message_shape():
    request = spike.build_request("Find times", "America/New_York", -240)

    assert request["jsonrpc"] == "2.0"
    assert request["method"] == "SendMessage"
    message = request["params"]["message"]
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": "Find times"}]
    assert message["metadata"]["Location"] == {
        "timeZoneOffset": -240,
        "timeZone": "America/New_York",
    }


def test_normalize_token_adds_auth_scheme_once():
    expected = spike.AUTH_SCHEME + " abc"
    assert spike.normalize_token("abc") == expected
    assert spike.normalize_token(expected) == expected


def test_normalize_token_rejects_empty_value():
    with pytest.raises(spike.WorkIQError, match="empty"):
        spike.normalize_token(" ")


def test_send_question_posts_once_with_required_headers(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(payload=completed_response())

    monkeypatch.setattr(spike.requests, "post", fake_post)
    result = spike.send_question("token", "Find times")

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == spike.WORKIQ_A2A_URL
    assert kwargs["headers"] == {
        "Authorization": spike.AUTH_SCHEME + " token",
        "Content-Type": "application/json",
        "A2A-Version": "1.0",
    }
    assert result["attendee"]["email"] == "rima.reyes@microsoft.com"


@pytest.mark.parametrize("status_code", [400, 401, 403, 500])
def test_send_question_surfaces_http_status(monkeypatch, status_code):
    monkeypatch.setattr(
        spike.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(status_code=status_code),
    )

    with pytest.raises(spike.WorkIQError, match=str(status_code)):
        spike.send_question("token", "Find times")


def test_send_question_surfaces_transport_error(monkeypatch):
    def fail(*args, **kwargs):
        raise spike.requests.Timeout("timed out")

    monkeypatch.setattr(spike.requests, "post", fail)

    with pytest.raises(spike.WorkIQError, match="timed out"):
        spike.send_question("token", "Find times")


def test_extract_artifact_requires_completed_task():
    payload = completed_response()
    payload["result"]["task"]["status"]["state"] = "TASK_STATE_WORKING"

    with pytest.raises(spike.WorkIQError, match="TASK_STATE_WORKING"):
        spike.extract_artifact(payload)


def test_extract_artifact_rejects_non_json_text():
    payload = completed_response()
    payload["result"]["task"]["artifacts"][0]["parts"][0]["text"] = "Here are times"

    with pytest.raises(spike.WorkIQError, match="strict JSON"):
        spike.extract_artifact(payload)


def test_extract_artifact_rejects_json_rpc_error():
    with pytest.raises(spike.WorkIQError, match="JSON-RPC"):
        spike.extract_artifact({"error": {"code": -1, "message": "failed"}})


def test_extract_artifact_rejects_missing_task():
    with pytest.raises(spike.WorkIQError, match="task status"):
        spike.extract_artifact({"result": {}})


def test_extract_artifact_rejects_missing_artifact():
    payload = completed_response()
    payload["result"]["task"]["artifacts"] = []

    with pytest.raises(spike.WorkIQError, match="no text artifact"):
        spike.extract_artifact(payload)


@pytest.mark.parametrize(
    "artifact, message",
    [
        ({"slots": [{"start": "x", "end": "y", "timeZone": "UTC"}]}, "attendee"),
        ({"attendee": {"name": "Rima"}, "slots": []}, "slot"),
        ({"attendee": {}, "slots": [{"start": "x"}]}, "name"),
        (
            {
                "attendee": {"name": "Rima"},
                "slots": [{"start": "x", "end": "y"}],
            },
            "timeZone",
        ),
    ],
)
def test_validate_schedule_rejects_incomplete_data(artifact, message):
    with pytest.raises(ValueError, match=message):
        spike.validate_schedule(artifact)


def test_acquire_token_uses_environment_without_msal(monkeypatch):
    monkeypatch.setenv("WORKIQ_ACCESS_TOKEN", "env-token")
    monkeypatch.setattr(
        spike.msal,
        "PublicClientApplication",
        lambda *args, **kwargs: pytest.fail("MSAL must not be created"),
    )

    assert spike.acquire_token(None, None) == "env-token"


def test_acquire_token_requires_app_configuration(monkeypatch):
    monkeypatch.delenv("WORKIQ_ACCESS_TOKEN", raising=False)

    with pytest.raises(spike.WorkIQError, match="WORKIQ_CLIENT_ID"):
        spike.acquire_token(None, None)


def test_acquire_token_uses_interactive_delegated_scope(monkeypatch):
    monkeypatch.delenv("WORKIQ_ACCESS_TOKEN", raising=False)
    observed = {}

    class FakeApp:
        def acquire_token_interactive(self, scopes):
            observed["scopes"] = scopes
            return {"access_token": "interactive-token"}

    monkeypatch.setattr(
        spike.msal,
        "PublicClientApplication",
        lambda client_id, authority: FakeApp(),
    )

    token = spike.acquire_token("client-id", "tenant-id")

    assert token == "interactive-token"
    assert observed["scopes"] == [spike.WORKIQ_SCOPE]


def test_acquire_token_surfaces_sign_in_failure(monkeypatch):
    monkeypatch.delenv("WORKIQ_ACCESS_TOKEN", raising=False)

    class FakeApp:
        def acquire_token_interactive(self, scopes):
            return {"error": "consent_required"}

    monkeypatch.setattr(
        spike.msal,
        "PublicClientApplication",
        lambda client_id, authority: FakeApp(),
    )

    with pytest.raises(spike.WorkIQError, match="consent_required"):
        spike.acquire_token("client-id", "tenant-id")


def test_run_spike_reports_latency_without_token(monkeypatch, capsys):
    monkeypatch.setattr(
        spike,
        "send_question",
        lambda token, question, **kwargs: schedule(),
    )

    report = spike.run_spike("super-secret", "Rima", "America/New_York", -240)
    output = capsys.readouterr().out

    assert report["elapsed_seconds"] >= 0
    assert report["request_count"] == 1
    assert report["result"]["slots"]
    assert "super-secret" not in output
