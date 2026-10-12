import io
import logging
import sys
from types import ModuleType, SimpleNamespace

import pytest

from laplace.logging import SafeFormatter, redact_text, safe_error_code, safe_event, setup_logging

CANARIES = (
    "configured-key-CANARY-private",
    "bearer-canary-0123456789",
    "owner.canary@example.test",
    "+84 912 345 678",
    "/Users/canary/private/document.txt",
    "https://owner:canary-password@example.test/private",
    "https://example.test/page?access_token=canary-query",
    "sk-canary0123456789abcdefghijkl",
    "123456789:canary_0123456789abcdefghijklmnop",
)


@pytest.fixture
def isolated_logging():
    root = logging.getLogger()
    loggers = [root] + [
        logger for logger in list(logging.Logger.manager.loggerDict.values())
        if isinstance(logger, logging.Logger)
    ]
    saved = [(logger, list(logger.handlers), logger.level, logger.propagate) for logger in loggers]
    formatters = {handler: handler.formatter for logger in loggers for handler in logger.handlers}
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]
    yield stream
    for logger, handlers, level, propagate in saved:
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
    for handler, formatter in formatters.items():
        handler.setFormatter(formatter)


def _record(message, args=(), exc_info=None):
    return logging.LogRecord("laplace.canary", logging.ERROR, "", 0, message, args, exc_info)


def test_formatted_mapping_arguments_and_configured_secrets_are_redacted():
    formatter = SafeFormatter("%(levelname)s %(message)s", secrets=(CANARIES[0],))
    detail = " | ".join((CANARIES[0], "Bearer " + CANARIES[1], *CANARIES[2:]))
    record = _record("tool_failure task_id=%(task_id)d detail=%(detail)s", {
        "task_id": 17,
        "detail": detail,
    })
    rendered = formatter.format(record)
    assert "ERROR tool_failure task_id=17" in rendered
    assert "[REDACTED]" in rendered
    for canary in CANARIES:
        assert canary not in rendered
    # Formatting must not rewrite data another canonical consumer still needs.
    assert record.args["detail"] == detail


def test_exception_chains_stack_and_cached_traceback_never_expose_private_data():
    formatter = SafeFormatter("%(message)s")
    try:
        try:
            raise ValueError("arbitrary private document body not identifiable by regex")
        except ValueError as cause:
            raise RuntimeError(" | ".join(CANARIES)) from cause
    except RuntimeError:
        record = _record("delivery_failed task_id=%s", (19,), sys.exc_info())
    record.exc_text = "cached private query parameters"
    record.stack_info = "private stack source code"
    rendered = formatter.format(record)
    assert rendered == "delivery_failed task_id=19 error_code=RuntimeError"
    assert record.exc_text == "cached private query parameters"
    assert record.stack_info == "private stack source code"
    assert record.exc_info is not None


def test_unknown_exception_class_name_is_not_an_operational_code():
    private_name = "private_customer_canary"
    private_error = type(private_name, (RuntimeError,), {})("private exception text")
    assert safe_error_code(private_error) == "runtime_error"
    assert safe_error_code(RuntimeError("secret")) == "RuntimeError"
    assert SafeFormatter("%(message)s").format(
        _record("worker_failed", exc_info=(type(private_error), private_error, None))
    ) == "worker_failed error_code=runtime_error"


def test_dependency_debug_body_is_omitted_at_root(isolated_logging):
    setup_logging(secrets=(CANARIES[0],), level=logging.DEBUG)
    logger = logging.getLogger("httpx.phase4_privacy_canary")
    try:
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
        logger.handlers = []
        logger.debug("request=%s body=%s", " | ".join(CANARIES), "private unclassified body")
        rendered = isolated_logging.getvalue()
        assert rendered == "DEBUG dependency dependency_log\n"
    finally:
        logger.handlers = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def test_setup_covers_existing_nonpropagating_handler_and_retains_both_secret_sets(
    isolated_logging,
):
    dependency = logging.getLogger("openai.phase4_privacy_canary")
    app = logging.getLogger("laplace.phase4_privacy_canary")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("local:%(levelname)s:%(message)s"))
    try:
        dependency.handlers = [handler]
        dependency.propagate = False
        dependency.setLevel(logging.DEBUG)
        app.handlers = []
        app.propagate = True
        app.setLevel(logging.NOTSET)
        setup_logging(secrets=("first-private-key",), level=logging.DEBUG)
        setup_logging(secrets=("second-private-key",), level=logging.DEBUG)
        dependency.debug("response %s", "private freeform response")
        app.info("error_details=%s", "first-private-key second-private-key")
        assert stream.getvalue() == "local:DEBUG:dependency_log\n"
        assert isolated_logging.getvalue() == (
            "INFO laplace.phase4_privacy_canary error_details=[REDACTED] [REDACTED]\n"
        )
    finally:
        dependency.handlers = []
        dependency.propagate = True
        dependency.setLevel(logging.NOTSET)
        app.handlers = []
        app.propagate = True
        app.setLevel(logging.NOTSET)


def test_safe_event_preserves_audit_linkage_and_metrics_without_private_payload():
    canonical = {
        "event": "tool_call", "task_id": 12, "step_index": 0, "approval_id": 4,
        "tool_call_id": 23, "tool": "create_reminder", "risk": "EXTERNAL_SEND",
        "status": "completed", "error_code": "timeout", "ok": False, "retryable": True,
        "latency_ms": 12.5, "physical_attempts": 2, "known_cost_usd": 0.001,
        "pricing_status": "settled", "stop_reason": "cost_limit", "overshoot": False,
        "telegram_user_id": 999999999, "username": "private owner", "model": "private model",
        "params": {"message": CANARIES[0]}, "result": " | ".join(CANARIES),
        "answer_preview": "private answer", "path": CANARIES[4],
    }
    projected = safe_event(canonical)
    assert projected == {
        "event": "tool_call", "task_id": 12, "step_index": 0, "approval_id": 4,
        "tool_call_id": 23, "tool_name": "create_reminder", "risk": "EXTERNAL_SEND",
        "status": "completed", "error_code": "timeout", "ok": False, "retryable": True,
        "latency_ms": 12.5, "physical_attempts": 2, "known_cost_usd": 0.001,
        "pricing_status": "settled", "stop_reason": "cost_limit", "overshoot": False,
    }
    assert canonical["params"] == {"message": CANARIES[0]}
    assert canonical["result"] == " | ".join(CANARIES)


def test_unknown_enum_strings_and_alias_precedence_cannot_smuggle_private_data():
    assert safe_event({
        "event": CANARIES[0], "tool_name": CANARIES[2], "tool": "web_search",
        "status": CANARIES[0], "error_code": "private_exception_class",
        "risk": "user_claimed_safe", "pricing_mode": CANARIES[0],
    }) == {"event": "unknown_event"}
    assert safe_event({"event": "awaiting_approval", "tool": "web_search"}) == {
        "event": "awaiting_approval", "tool_name": "web_search",
    }


@pytest.mark.parametrize("value", [True, False, -1, 10**40, float("nan"), float("inf"), "17"])
def test_numeric_projection_rejects_unbounded_or_non_numeric_values(value):
    assert safe_event({"task_id": value, "step_index": value, "latency_ms": value}) == {}


def test_numeric_projection_keeps_finite_boundary_values_and_distinguishes_booleans():
    assert safe_event({
        "task_id": 2**63 - 1, "step_index": 0, "latency_ms": 0.0, "ok": 1,
        "retryable": "true", "physical_attempts": 10**9,
    }) == {"task_id": 2**63 - 1, "step_index": 0, "latency_ms": 0.0,
           "physical_attempts": 10**9}


def test_redactor_handles_token_assignments_windows_paths_and_preserves_public_urls():
    text = (
        "api_key=unconfigured-canary token: 'token-canary' "
        "C:\\Users\\private\\document.txt https://public.example.test/article?id=42"
    )
    rendered = redact_text(text)
    assert "unconfigured-canary" not in rendered
    assert "token-canary" not in rendered
    assert "C:\\Users\\private\\document.txt" not in rendered
    assert "https://public.example.test/article?id=42" in rendered

@pytest.mark.parametrize("url", [
    "https://example.test/page?%74oken=encoded-canary",
    "https://example.test/page?API_KEY=private-canary",
    "https://example.test/page?telegram_user_id=1234567",
    "https://owner:private-canary@example.test/page",
    "https://example.test/private/owner@example.test/document",
    "file:///Users/private/evidence.txt",
])
def test_sensitive_url_is_omitted_whole_instead_of_exposing_a_partial_reference(url):
    assert redact_text(url) == "[REDACTED]"


def test_configured_secret_in_url_path_omits_the_whole_reference():
    source = "https://example.test/docs/private-owner/evidence"
    assert redact_text(source, secrets=("private-owner",)) == "[REDACTED]"


def test_startup_installs_privacy_before_bot_logs(monkeypatch, isolated_logging):
    from laplace import __main__ as entrypoint

    settings = SimpleNamespace(
        gemini_api_key=CANARIES[0], openai_api_key="", groq_api_key="", bai_api_key="",
        router9_api_key="", telegram_bot_token="", tavily_api_key="", log_level="DEBUG",
    )
    monkeypatch.setattr(entrypoint, "Settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["laplace", "--bot"])
    runner = ModuleType("laplace.bot.runner")

    def start_bot():
        logging.getLogger("laplace.startup_canary").warning("startup_failed %s", CANARIES[0])
        logging.getLogger("httpx.startup_canary").debug("private response body")

    runner.main = start_bot
    monkeypatch.setitem(sys.modules, "laplace.bot.runner", runner)
    entrypoint.main()
    rendered = isolated_logging.getvalue()
    assert "startup_failed [REDACTED]" in rendered
    assert "dependency_log" in rendered
    assert CANARIES[0] not in rendered
    assert "private response body" not in rendered
