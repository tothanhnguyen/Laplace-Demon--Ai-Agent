"""Actual local HTTP contracts for capped physical attempts and accounting."""

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from sqlalchemy import select

from laplace import repo
from laplace.config import Settings
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.base import BudgetStop, LLMResult, get_provider
from laplace.llm.mock import MockLLM, account_mock_result
from laplace.llm.openai_provider import OpenAIProvider
from laplace.llm.presets import PRESETS
from laplace.models import LLMCall
from laplace.services.budget import AttemptRecorder, TurnBudget

MESSAGES = [{"role": "user", "content": "private-prompt-đề-nghị"}]
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


def _response(
    model: str = "gpt-4o-mini-2024-07-18",
    *,
    usage: dict[str, Any] | None = None,
    missing_usage: bool = False,
) -> dict[str, Any]:
    body = {
        "id": "chatcmpl-contract",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"answer":"ok"}'},
                "finish_reason": "stop",
            }
        ],
    }
    if not missing_usage:
        body["usage"] = usage or {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    return body


def _rows() -> list[LLMCall]:
    with session_scope() as session:
        return list(session.scalars(select(LLMCall).order_by(LLMCall.id)))


class _Endpoint:
    def __init__(self, replies: list[dict[str, Any]]):
        self.replies = replies
        self.requests: list[dict[str, Any]] = []
        self.reserved_at_send: list[tuple[str, float]] = []
        self.disconnected = threading.Event()
        self.errors: list[Exception] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *args):
                pass

            def do_POST(self):
                try:
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    endpoint.requests.append(payload)
                    row = _rows()[-1]
                    endpoint.reserved_at_send.append(
                        (row.accounting_status, row.budget_charge_usd)
                    )
                    reply = endpoint.replies.pop(0)
                    body = json.dumps(reply["body"]).encode()
                    self.send_response(reply.get("status", 200))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    drip = reply.get("drip", 0)
                    if drip:
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(drip)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    endpoint.disconnected.set()
                except Exception as error:
                    endpoint.errors.append(error)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)


@pytest.fixture(autouse=True)
def database() -> Iterator[None]:
    reset_engine_for_tests("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_tests(None)


@pytest.fixture()
def endpoint():
    opened: list[_Endpoint] = []

    def create(replies: list[dict[str, Any]]) -> _Endpoint:
        server = _Endpoint(replies)
        opened.append(server)
        return server

    yield create
    for server in opened:
        server.close()


def _budget(**overrides):
    defaults = {
        "_env_file": None,
        "max_seconds": 10,
        "max_cost_per_task": Decimal("0.10"),
        "llm_max_completion_tokens": 128,
        "llm_timeout_seconds": 2,
    }
    defaults.update(overrides)
    budget = TurnBudget(Settings(**defaults))
    recorder = AttemptRecorder(budget, user_id=None, request_message_id=None)
    return budget, budget.call_options(recorder)


def _provider(server: _Endpoint, *, name="openai", model=None):
    preset = PRESETS[name]
    return OpenAIProvider(
        api_key="local-contract-key", model=model or preset.default_model,
        name=name, base_url=server.base_url, pricing=preset.pricing,
        output_cap_parameter=preset.output_cap_parameter, pricing_version=preset.pricing_version,
    )


def _reservation(payload: dict[str, Any], name="openai") -> Decimal:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    estimate = len(encoded) + 256 + 32 * len(payload["messages"])
    preset = PRESETS[name]
    input_rate, output_rate = (Decimal(str(value)) for value in preset.pricing[payload["model"]])
    return (
        estimate * input_rate + payload[preset.output_cap_parameter] * output_rate
    ) / Decimal(1_000_000)


def test_paid_reservation_is_committed_before_http_then_settles_exact_alias(endpoint):
    server = endpoint([{"body": _response()}])
    budget, options = _budget()
    result = _provider(server).complete(MESSAGES, options=options, json_schema=SCHEMA)
    row, = _rows()
    payload, = server.requests
    assert not server.errors
    assert payload["max_tokens"] == 128
    assert payload["response_format"] == {"type": "json_object"}
    assert json.dumps(SCHEMA) in payload["messages"][-1]["content"]
    assert server.reserved_at_send == [("reserved", pytest.approx(float(_reservation(payload))))]
    assert result.parsed == {"answer": "ok"}
    assert result.attempt_ids == (row.id,)
    assert result.model == "gpt-4o-mini-2024-07-18"
    assert row.accounting_status == "settled"
    expected_cost = Decimal("0.0000036")
    assert row.cost_usd == pytest.approx(float(expected_cost))
    assert budget.budget_charge_usd == expected_cost
    assert budget.known_cost_usd == expected_cost
    detail = json.loads(row.accounting_detail)
    assert detail["pricing_version"] == PRESETS["openai"].pricing_version
    assert detail["usage_known"] is True
    assert "private-prompt" not in row.accounting_detail
    assert budget.unknown_attempts == 0


@pytest.mark.parametrize(
    "name,model",
    [(name, model) for name, preset in PRESETS.items() for model in preset.pricing],
)
def test_every_priced_preset_model_remains_capped_and_accountable(endpoint, name, model):
    server = endpoint([{"body": _response(model)}])
    budget, options = _budget(max_cost_per_task=Decimal("10"))
    result = _provider(server, name=name, model=model).complete(MESSAGES, options=options)
    payload, = server.requests
    row, = _rows()
    assert payload[PRESETS[name].output_cap_parameter] == 128
    other_cap = (
        "max_tokens" if PRESETS[name].output_cap_parameter == "max_completion_tokens"
        else "max_completion_tokens"
    )
    assert other_cap not in payload
    assert result.model == model and row.accounting_status == "settled"
    assert budget.unknown_attempts == 0
    assert not server.errors


@pytest.mark.parametrize("unknown", ["missing_usage", "unpriced_alias", "negative_usage"])
def test_ambiguous_success_retains_paid_reserve_and_blocks_next_request(endpoint, unknown):
    body = _response(
        "unpriced-returned-alias" if unknown == "unpriced_alias" else "gpt-4o-mini",
        missing_usage=unknown == "missing_usage",
        usage={"prompt_tokens": -1, "completion_tokens": 3, "total_tokens": 2}
        if unknown == "negative_usage" else None,
    )
    server = endpoint([{"body": body}, {"body": _response()}])
    budget, options = _budget()
    provider = _provider(server)
    with pytest.raises(BudgetStop) as stopped:
        provider.complete(MESSAGES, options=options)
    assert stopped.value.reason == "budget_unavailable"
    row, = _rows()
    assert row.accounting_status == "unknown"
    assert row.cost_usd == 0
    assert row.budget_charge_usd == pytest.approx(float(_reservation(server.requests[0])))
    assert budget.known_cost_usd == 0 and budget.unknown_attempts == 1
    with pytest.raises(BudgetStop):
        provider.complete(MESSAGES, options=options)
    assert len(server.requests) == 1


def test_known_zero_alias_is_not_the_same_as_unknown_usage(endpoint):
    server = endpoint([
        {"body": _response("gpt-5.6-sol")},
        {"body": _response("gpt-5.6-sol", missing_usage=True)},
    ])
    budget, options = _budget(max_cost_per_task=Decimal(0))
    provider = _provider(server, name="router9")
    result = provider.complete(MESSAGES, options=options)
    assert result.model == "gpt-5.6-sol" and result.cost_usd == 0
    with pytest.raises(BudgetStop) as stopped:
        provider.complete(MESSAGES, options=options)
    assert stopped.value.reason == "budget_unavailable"
    assert [row.accounting_status for row in _rows()] == ["settled", "unknown"]
    assert budget.budget_charge_usd == 0 and budget.unknown_attempts == 1
    assert all(request["max_completion_tokens"] == 128 for request in server.requests)


def test_paid_cost_admission_rejects_before_network_or_attempt_row(endpoint):
    server = endpoint([{"body": _response()}])
    _, options = _budget(max_cost_per_task=Decimal(0))
    with pytest.raises(BudgetStop) as stopped:
        _provider(server).complete(MESSAGES, options=options)
    assert stopped.value.reason == "cost_limit"
    assert server.requests == [] and _rows() == []


def test_lower_call_cap_is_reserved_from_actual_payload_not_budget_default(endpoint):
    server = endpoint([{"body": _response()}])
    _, options = _budget()
    options = replace(options, max_completion_tokens=32)
    _provider(server).complete(MESSAGES, options=options)
    payload, = server.requests
    assert payload["max_tokens"] == 32
    assert server.reserved_at_send == [("reserved", pytest.approx(float(_reservation(payload))))]


def test_retained_rate_limit_charge_rejects_next_attempt_before_send(endpoint):
    server = endpoint([{
        "status": 429, "body": {"error": {"message": "retry in 0s", "type": "rate_limit"}},
    }])
    payload = {"model": "gpt-4o-mini", "messages": MESSAGES, "max_tokens": 128}
    _, options = _budget(max_cost_per_task=_reservation(payload) * Decimal("1.5"))
    with pytest.raises(BudgetStop) as stopped:
        _provider(server).complete(MESSAGES, options=options)
    assert stopped.value.reason == "cost_limit"
    assert len(server.requests) == 1
    row, = _rows()
    assert row.accounting_status == "unknown"
    assert row.budget_charge_usd == pytest.approx(float(_reservation(server.requests[0])))


def test_settled_overshoot_stops_before_result_or_second_network_request(endpoint):
    server = endpoint([{"body": _response(
        usage={"prompt_tokens": 10_000, "completion_tokens": 3, "total_tokens": 10_003}
    )}])
    budget, options = _budget()
    provider = _provider(server)
    with pytest.raises(BudgetStop) as stopped:
        provider.complete(MESSAGES, options=options)
    assert stopped.value.reason == "cost_limit"
    row, = _rows()
    assert row.accounting_status == "settled" and row.prompt_tokens == 10_000
    assert budget.overshoot is True
    assert row.budget_charge_usd == pytest.approx(0.0015018)
    with pytest.raises(BudgetStop):
        provider.complete(MESSAGES, options=options)
    assert len(server.requests) == 1


def test_rate_limit_and_format_fallback_each_retain_an_independent_reservation(endpoint):
    server = endpoint([
        {"status": 429, "body": {"error": {"message": "retry in 0s", "type": "rate_limit"}}},
        {"status": 400, "body": {"error": {
            "message": "response_format is unsupported", "param": "response_format",
        }}},
        {"body": _response()},
    ])
    budget, options = _budget()
    result = _provider(server).complete(MESSAGES, options=options, json_schema=SCHEMA)
    rows = _rows()
    assert len(server.requests) == 3 and not server.errors
    assert [row.attempt_index for row in rows] == [0, 1, 2]
    assert len({row.call_group_id for row in rows}) == 1
    assert result.attempt_ids == tuple(row.id for row in rows)
    assert [json.loads(row.accounting_detail)["outcome"] for row in rows] == [
        "rate_limit", "format_rejected", "success",
    ]
    assert [row.accounting_status for row in rows] == ["unknown", "unknown", "settled"]
    assert all(payload["max_tokens"] == 128 for payload in server.requests)
    assert "response_format" in server.requests[0]
    assert "response_format" in server.requests[1]
    assert "response_format" not in server.requests[2]
    retained = sum((_reservation(payload) for payload in server.requests[:2]), Decimal(0))
    assert budget.budget_charge_usd == retained + Decimal("0.0000036")
    with session_scope() as session:
        usage = repo.total_usage(session)
    assert usage["llm_calls"] == 1 and usage["physical_attempts"] == 3
    assert usage["unknown_attempts"] == 2
    assert usage["budget_charge_usd"] == pytest.approx(float(budget.budget_charge_usd))


def test_backoff_is_clamped_to_actual_task_deadline_without_another_attempt(endpoint):
    server = endpoint([{
        "status": 429, "body": {"error": {"message": "retry in 30s", "type": "rate_limit"}},
    }])
    budget, options = _budget(max_seconds=2)
    started = time.monotonic()
    with pytest.raises(BudgetStop) as stopped:
        _provider(server).complete(MESSAGES, options=options)
    assert stopped.value.reason == "time_limit"
    assert time.monotonic() - started < 3.5
    assert len(server.requests) == 1 and not server.errors
    row, = _rows()
    assert row.accounting_status == "unknown" and row.budget_charge_usd > 0
    assert budget.physical_attempts == 1


def test_slow_drip_is_cancelled_by_total_attempt_deadline_not_socket_timeout(endpoint):
    server = endpoint([{"body": _response(), "drip": 0.02}])
    budget, options = _budget()
    options = replace(options, attempt_timeout_seconds=0.3)
    started = time.monotonic()
    with pytest.raises(BudgetStop) as stopped:
        _provider(server).complete(MESSAGES, options=options)
    assert stopped.value.reason == "budget_unavailable"
    assert time.monotonic() - started < 2
    assert server.disconnected.wait(1)
    assert len(server.requests) == 1 and not server.errors
    row, = _rows()
    assert row.accounting_status == "unknown"
    assert json.loads(row.accounting_detail)["outcome"] == "timeout"
    assert row.latency_ms >= 200
    assert budget.budget_charge_usd > 0


@pytest.mark.parametrize("status", [400, 500])
def test_no_hidden_retry_or_uncapped_fallback_on_unrelated_backend_error(endpoint, status):
    server = endpoint([{"status": status, "body": {"error": {
        "message": "max_tokens unsupported; secret-provider-body", "param": "max_tokens",
    }}}])
    budget, options = _budget()
    expected_error = ValueError if status == 400 else BudgetStop
    with pytest.raises(expected_error) as stopped:
        _provider(server).complete(MESSAGES, options=options, json_schema=SCHEMA)
    assert "secret-provider-body" not in str(stopped.value)
    assert len(server.requests) == 1 and server.requests[0]["max_tokens"] == 128
    row, = _rows()
    assert row.accounting_status == "unknown" and row.budget_charge_usd > 0
    with pytest.raises(BudgetStop):
        budget.check()


def test_durable_reservation_failure_never_sends_http(endpoint, monkeypatch):
    server = endpoint([{"body": _response()}])
    _, options = _budget()

    def fail_reservation(*args, **kwargs):
        raise RuntimeError("secret-SQL-and-token")

    monkeypatch.setattr(repo, "record_llm_call", fail_reservation)
    with pytest.raises(BudgetStop) as stopped:
        _provider(server).complete(MESSAGES, options=options)
    assert stopped.value.reason == "budget_unavailable"
    assert "secret-SQL" not in str(stopped.value)
    assert server.requests == [] and _rows() == []


def test_settlement_failure_retains_reserved_row_and_blocks_next_network(endpoint, monkeypatch):
    server = endpoint([{"body": _response()}])
    _, options = _budget()
    provider = _provider(server)

    def fail_settlement(*args, **kwargs):
        raise RuntimeError("secret-SQL-and-token")

    monkeypatch.setattr(repo, "finalize_llm_attempt", fail_settlement)
    with pytest.raises(BudgetStop) as stopped:
        provider.complete(MESSAGES, options=options)
    assert stopped.value.reason == "budget_unavailable"
    assert "secret-SQL" not in str(stopped.value)
    row, = _rows()
    assert row.accounting_status == "reserved" and row.budget_charge_usd > 0
    with pytest.raises(BudgetStop):
        provider.complete(MESSAGES, options=options)
    assert len(server.requests) == 1


def test_explicit_mock_and_context_fakes_use_real_known_zero_accounting():
    budget, options = _budget(max_cost_per_task=Decimal(0))
    provider = MockLLM(["first"])
    first = provider.complete(MESSAGES, options=options)
    second = account_mock_result(
        replace(first, content="custom", model="context-fake", cost_usd=4.5),
        MESSAGES, options=options, provider="evaluation-fake",
    )
    assert second.content == "custom" and second.cost_usd == 0
    assert second.attempt_ids != first.attempt_ids
    assert [row.accounting_status for row in _rows()] == ["settled", "settled"]
    assert budget.budget_charge_usd == 0 and budget.unknown_attempts == 0
    with session_scope() as session:
        usage = repo.total_usage(session)
    assert usage["physical_attempts"] == 2 and usage["llm_calls"] == 2
    assert usage["completion_tokens"] == 20


def test_mock_missing_usage_does_not_become_a_free_success():
    budget, options = _budget(max_cost_per_task=Decimal(0))
    provider = MockLLM([LLMResult(content="unknown", extra={"usage_known": False})])
    with pytest.raises(BudgetStop) as stopped:
        provider.complete(MESSAGES, options=options)
    assert stopped.value.reason == "budget_unavailable"
    row, = _rows()
    assert row.accounting_status == "unknown" and budget.unknown_attempts == 1


def test_unknown_provider_is_configuration_error_and_mock_is_explicit():
    assert isinstance(get_provider("mock"), MockLLM)
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        get_provider("misspelled-provider")


@pytest.mark.parametrize("model,cap", [
    ("unpriced-model", "max_tokens"), ("gpt-4o-mini", "unsupported_cap"),
])
def test_unsupported_model_or_cap_profile_fails_before_network(endpoint, model, cap):
    server = endpoint([{"body": _response()}])
    with pytest.raises(ValueError):
        OpenAIProvider(
            api_key="local-contract-key", model=model, base_url=server.base_url,
            pricing=PRESETS["openai"].pricing, output_cap_parameter=cap,
        )
    assert server.requests == [] and _rows() == []
