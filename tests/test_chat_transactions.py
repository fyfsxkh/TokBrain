import json

import pytest

import app.routers_v2.chat as chat
from app.schemas import ChatRequest
from app.services.providers import ProviderUsage


def _sources():
    return [
        {
            "work_id": 1,
            "platform_work_id": "work-1",
            "title": "title",
            "text": "grounded source",
            "source_kind": "transcript",
        }
    ]


class CommitSession:
    def __init__(self, failing_calls=()):
        self.commit_calls = 0
        self.failing_calls = set(failing_calls)
        self.rollback_calls = 0

    async def commit(self):
        self.commit_calls += 1
        if self.commit_calls in self.failing_calls:
            raise RuntimeError("forced commit failure")

    async def rollback(self):
        self.rollback_calls += 1


async def _events(response):
    payloads = []
    async for chunk in response.body_iterator:
        payloads.append(json.loads(chunk))
    return payloads


async def test_ask_commit_failure_consumes_completed_call_instead_of_releasing(
    monkeypatch,
):
    session = CommitSession(failing_calls={2})
    usage = ProviderUsage("qwen-test", 4, 3, 0, quantity=7)
    records = []
    consumed = []
    released = []

    class Provider:
        async def chat(self, *_args, **_kwargs):
            return "answer", usage

    monkeypatch.setattr(chat, "get_secret", lambda *_args: _value("key"))
    monkeypatch.setattr(chat, "_retrieve", lambda *_args: _value(_sources()))
    monkeypatch.setattr(
        chat, "get_runtime_settings", lambda *_args: _value({"chat_fast_model": "m"})
    )
    monkeypatch.setattr(chat, "DashScopeProvider", lambda _key: Provider())
    monkeypatch.setattr(chat, "reserve", lambda *_args, **_kwargs: _value(object()))

    async def record(*_args, **kwargs):
        records.append(kwargs["metadata"])

    async def consume(*_args, **kwargs):
        consumed.append(kwargs["actual_llm_tokens"])

    async def release(*_args, **_kwargs):
        released.append(True)

    monkeypatch.setattr(chat, "record_usage", record)
    monkeypatch.setattr(chat, "consume", consume)
    monkeypatch.setattr(chat, "release", release)

    with pytest.raises(RuntimeError, match="forced commit failure"):
        await chat.ask(ChatRequest(question="question"), session)  # type: ignore[arg-type]

    assert consumed == [7, 7]
    assert not released
    assert records[-1]["recovered_after_commit_failure"] is True
    assert session.rollback_calls == 1


async def test_stream_reservation_commit_failure_does_not_release_other_reservations(
    monkeypatch,
):
    session = CommitSession(failing_calls={1})
    released = []
    monkeypatch.setattr(chat, "get_secret", lambda *_args: _value("key"))
    monkeypatch.setattr(chat, "_retrieve", lambda *_args: _value(_sources()))
    monkeypatch.setattr(
        chat, "get_runtime_settings", lambda *_args: _value({"chat_fast_model": "m"})
    )
    monkeypatch.setattr(chat, "reserve", lambda *_args, **_kwargs: _value(object()))
    monkeypatch.setattr(
        chat, "release", lambda *_args, **_kwargs: _record(released, True)
    )

    response = await chat.ask_stream(
        ChatRequest(question="question"), session  # type: ignore[arg-type]
    )
    events = await _events(response)

    assert "done" not in [item["type"] for item in events]
    assert events[-1]["type"] == "error"
    assert not released


async def test_stream_empty_result_commit_happens_before_terminal_events(monkeypatch):
    session = CommitSession(failing_calls={1})
    monkeypatch.setattr(chat, "get_secret", lambda *_args: _value("key"))
    monkeypatch.setattr(chat, "_retrieve", lambda *_args: _value([]))

    response = await chat.ask_stream(
        ChatRequest(question="question"), session  # type: ignore[arg-type]
    )
    events = await _events(response)

    assert [item["type"] for item in events] == ["stage", "error"]


async def _value(value):
    return value


async def _record(target, value):
    target.append(value)
