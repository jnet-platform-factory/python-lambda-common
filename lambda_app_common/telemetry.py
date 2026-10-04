"""Per-invocation logging keys, the `api_request` metric, and the one-line event summary.

These used to be methods on a handler object that every Lambda built at import time. They
are plain functions now so both the HTTP middleware chain and the event middleware can share
them, and so they can be tested without constructing anything.

Telemetry never fails an invocation: everything that writes to a logger or a metrics object
is wrapped, and a failure is printed and swallowed.
"""

import json
import os
from typing import Any, Mapping, Optional

## The logger metadata is fed to `logger.append_keys`, so whatever it holds is repeated on
## EVERY log line for the rest of the invocation -- a persistent key costs its own size
## multiplied by the number of lines written, not once. An EventBridge `detail` is
## caller-supplied and unbounded, which makes that product unbounded too: on 2026-09-17 one
## event with a few thousand items in its detail turned one-line-per-item logging into 88k
## lines and 12.9GB in a single hour, against ~95KB on an ordinary day. Anything attached
## persistently has to be small and stay small.
DETAIL_LOG_MAX_CHARS = 2048


def bounded_detail(detail):
    """The event detail if it is small enough to repeat on every line, else a summary.

    Small events -- the overwhelming majority -- are unchanged, so the debugging value is
    kept. A large one is replaced by its shape: the keys present and how big it was, which
    is what you actually need to go and find the original event.
    """
    if detail is None:
        return None
    try:
        rendered = json.dumps(detail, default=str)
    except (TypeError, ValueError):
        return {"truncated": True, "reason": "not JSON-serialisable"}

    if len(rendered) <= DETAIL_LOG_MAX_CHARS:
        return detail

    return {
        "truncated": True,
        "size_chars": len(rendered),
        "keys": sorted(detail)[:50] if isinstance(detail, dict) else None,
    }


## The name it had as a handler method; kept so the behaviour is findable by it.
_bounded_detail = bounded_detail


def emf_safe(value, _depth=0):
    """Make a value survive the EMF `json.dumps` at the end of the invocation.

    Powertools flushes metrics from the `log_metrics` decorator, i.e. *after* the handler
    has returned, and `flush_metrics` calls `json.dumps` on everything that was handed to
    `add_metadata`. Anything it cannot encode raises there -- long after the work is done,
    where the traceback names only Powertools frames. Lambda still records the invocation
    as failed, so an EventBridge or SQS source will redeliver work that already succeeded.

    Handlers routinely enrich an event's `detail` in place with values read from the
    database -- a `date`, a `Decimal`, an ORM object -- and the detail is in the metadata.
    That is how a telemetry line came to fail real invocations
    (`TypeError: Object of type date is not JSON serializable`).

    Anything not JSON-encodable becomes its `str()`. Telemetry is worth a lossy value; it
    is never worth an invocation.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if _depth >= 6:
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): emf_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [emf_safe(v, _depth + 1) for v in value]
    return str(value)


_emf_safe = emf_safe


def debug_enabled() -> bool:
    return os.environ.get("DEBUG", "0") not in ("0", "", "false", "False")


def http_logger_metadata(event: Mapping, user_context: Any, stage: Optional[str]) -> dict:
    """The keys appended to every log line of an API Gateway invocation."""
    event = event or {}
    return {
        "env": stage,
        "organization": getattr(user_context, "organization", None),
        "username": getattr(user_context, "username", None),
        "email": getattr(user_context, "user_email", None),
        "user_groups": getattr(user_context, "user_groups", None),
        "method": event.get("httpMethod"),
        "resource": event.get("resource"),
        "seller_id": getattr(user_context, "seller_id", None),
        "customer_id": getattr(user_context, "customer_id", None),
        "query_params": event.get("queryStringParameters", {}),
    }


def metrics_dimensions(stage: Optional[str], organization: Optional[str], method: Optional[str] = None) -> dict:
    return {
        "env": stage,
        "organization": organization,
        "read_only": method in ("GET", "OPTIONS") if method else False,
    }


def append_logger_keys(logger: Any, metadata: Mapping) -> None:
    if logger is None or not hasattr(logger, "append_keys"):
        return
    try:
        logger.append_keys(**metadata)
    except Exception as e:  # noqa: BLE001 - telemetry must not fail the invocation
        print(f"[telemetry] append_keys failed: {e}")


def record_api_request(metrics: Any, dimensions: Mapping, metadata: Mapping) -> None:
    """Add the `api_request` count, its dimensions, and the invocation's metadata."""
    if metrics is None or not hasattr(metrics, "add_metric"):
        return
    try:
        from aws_lambda_powertools.metrics import MetricUnit

        for key, value in dimensions.items():
            metrics.add_dimension(name=key, value=str(value))
        metrics.add_metric(name="api_request", unit=MetricUnit.Count, value=1)
        for key, value in metadata.items():
            metrics.add_metadata(key=key, value=emf_safe(value))
    except Exception as e:  # noqa: BLE001 - telemetry must not fail the invocation
        print(f"[telemetry] api_request metric failed: {e}")


def print_request_summary(service: Optional[str], event: Mapping) -> None:
    print(f"[REQUEST] {service or '-'} | {event.get('httpMethod')} {event.get('resource')}")
