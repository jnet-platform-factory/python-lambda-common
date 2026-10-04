"""The event-side counterpart of the HTTP middleware chain: EventBridge, SQS and SES.

Wrap the Lambda entry point, inside the logger decorator and outside any Powertools
`event_source` / batch decorator, so it sees the raw event:

    @logger.inject_lambda_context(log_event=True)
    @event_context(service="Invoices", logger=service.logger, metrics=service.metrics)
    @event_source(data_class=EventBridgeEvent)
    def handler(event, context):
        organization = current_invocation().organization
        ...

For every invocation it:
  1. runs the configured `on_invocation_start` hooks with the raw event -- this is where a
     service resets per-invocation state that a warm container would otherwise carry over,
     such as its EventBridge loop-detection trail, and seeds it from the incoming event;
  2. classifies the event and exposes it as `current_invocation()`;
  3. prints one summary line (the whole bounded event under DEBUG);
  4. appends the invocation's keys to `logger` (detail bounded to DETAIL_LOG_MAX_CHARS);
  5. records the `api_request` metric on `metrics`, when one is passed.

It never fails the invocation for telemetry. A failing start hook does fail it.
"""

import functools
import json
from dataclasses import dataclass, field
from typing import Any, Callable, List, Mapping, Optional

from .. import telemetry
from ..config import get_config, run_invocation_start_hooks

EVENTBRIDGE = "eventbridge"
SQS = "sqs"
SES = "ses"
HTTP = "http"
UNKNOWN = "unknown"


@dataclass
class EventInvocation:
    kind: str
    raw_event: Any = None
    service: Optional[str] = None
    source: Optional[str] = None
    detail_type: Optional[str] = None
    detail: Any = None
    organization: Optional[str] = None
    username: Optional[str] = None
    message_id: Optional[str] = None
    event_source_arn: Optional[str] = None
    payload: Any = None
    ses_message_id: Optional[str] = None
    ses_source: Optional[str] = None
    ses_destination: List[str] = field(default_factory=list)
    ses_subject: Optional[str] = None

    @property
    def is_warmup(self) -> bool:
        return self.kind == EVENTBRIDGE and "warmup" in (self.source or "")


def raw_event_of(event: Any) -> Any:
    """The plain dict behind a Powertools data class, or the event itself."""
    return getattr(event, "raw_event", event)


def _organization_from_sender(sender: Any) -> Optional[str]:
    """SES mail is attributed to the sender address' local part, alphanumerics only, lowercased."""
    if isinstance(sender, str) and "@" in sender:
        local = sender.strip().split("@", 1)[0]
        return "".join(ch for ch in local if ch.isascii() and ch.isalnum()).lower()
    return None


def classify_event(event: Any, service: Optional[str] = None) -> EventInvocation:
    raw = raw_event_of(event)
    if not isinstance(raw, Mapping):
        return EventInvocation(kind=UNKNOWN, raw_event=raw, service=service)

    if "httpMethod" in raw:
        return EventInvocation(kind=HTTP, raw_event=raw, service=service)

    if "source" in raw and "detail" in raw:
        detail = raw.get("detail")
        detail_map = detail if isinstance(detail, Mapping) else {}
        return EventInvocation(
            kind=EVENTBRIDGE, raw_event=raw, service=service,
            source=raw.get("source"), detail_type=raw.get("detail-type"), detail=detail,
            organization=detail_map.get("organization"), username=detail_map.get("username"),
        )

    records = raw.get("Records")
    if isinstance(records, list) and records:
        first = records[0] if isinstance(records[0], Mapping) else {}
        if first.get("eventSource") == "aws:ses":
            mail = (first.get("ses") or {}).get("mail") or {}
            return EventInvocation(
                kind=SES, raw_event=raw, service=service,
                ses_message_id=mail.get("messageId"), ses_source=mail.get("source"),
                ses_destination=list(mail.get("destination") or []),
                ses_subject=(mail.get("commonHeaders") or {}).get("subject"),
                organization=_organization_from_sender(mail.get("source")),
            )

        payload = None
        try:
            payload = json.loads(first.get("body") or "null")
        except (TypeError, ValueError):
            pass
        payload_map = payload if isinstance(payload, Mapping) else {}
        return EventInvocation(
            kind=SQS, raw_event=raw, service=service,
            message_id=first.get("messageId"), event_source_arn=first.get("eventSourceARN"),
            payload=payload,
            organization=payload_map.get("organization"), username=payload_map.get("username"),
        )

    return EventInvocation(kind=UNKNOWN, raw_event=raw, service=service)


def event_logger_metadata(invocation: EventInvocation, stage: Optional[str],
                          application: Optional[str]) -> dict:
    """The keys appended to every log line of an event invocation."""
    base = {"application": application, "env": stage}
    if invocation.kind == EVENTBRIDGE:
        return {
            **base,
            "source": invocation.source,
            "detail_type": invocation.detail_type,
            "detail": telemetry.bounded_detail(invocation.detail),
            "organization": invocation.organization,
            "username": invocation.username,
        }
    if invocation.kind == SES:
        return {
            **base,
            "ses_message_id": invocation.ses_message_id,
            "ses_source": invocation.ses_source,
            "ses_destination": invocation.ses_destination,
            "organization": invocation.organization,
            "username": invocation.username,
        }
    if invocation.kind == SQS:
        return {
            **base,
            "organization": invocation.organization,
            "username": invocation.username,
            "sqs_message_id": invocation.message_id,
            "sqs_event_source_arn": invocation.event_source_arn,
        }
    return {**base, "organization": invocation.organization, "username": invocation.username}


def print_event_summary(invocation: EventInvocation) -> None:
    service = invocation.service or "-"
    if invocation.kind == EVENTBRIDGE:
        print(f"[EVENT] {service} | {invocation.source} -> {invocation.detail_type}")
    elif invocation.kind == SES:
        print(f"[SES] {service} | {invocation.ses_message_id or 'unknown'}")
    elif invocation.kind == SQS:
        print(f"[SQS] {service} | {invocation.message_id or 'unknown'}")
    elif invocation.kind == HTTP:
        telemetry.print_request_summary(service, invocation.raw_event)
    else:
        print(f"[EVENT] {service} | unknown event type")

    if telemetry.debug_enabled():
        try:
            rendered = json.dumps(telemetry.bounded_detail(invocation.raw_event), default=str)
        except (TypeError, ValueError):
            rendered = "<not JSON-serialisable>"
        print(f"[EVENT] {service} | event: {rendered}")


_current: Optional[EventInvocation] = None


def current_invocation() -> Optional[EventInvocation]:
    """The invocation `event_context` is currently wrapping, or None outside one."""
    return _current


def start_event_invocation(event: Any, *, service: Optional[str] = None, logger: Any = None,
                           metrics: Any = None) -> EventInvocation:
    """Everything `event_context` does before calling the handler, for entry points that
    cannot take a decorator. Pair with `end_event_invocation()`."""
    global _current
    raw = raw_event_of(event)
    run_invocation_start_hooks(raw)

    invocation = classify_event(raw, service)
    _current = invocation
    config = get_config()
    stage = config.resolved_stage()
    try:
        print_event_summary(invocation)
    except Exception as e:  # noqa: BLE001 - telemetry must not fail the invocation
        print(f"[telemetry] event summary failed: {e}")
    metadata = event_logger_metadata(invocation, stage, config.resolved_application())
    telemetry.append_logger_keys(logger, metadata)
    if metrics is not None:
        telemetry.record_api_request(metrics, telemetry.metrics_dimensions(stage, invocation.organization), metadata)
    return invocation


def end_event_invocation() -> None:
    global _current
    _current = None


def event_context(handler: Optional[Callable] = None, *, service: Optional[str] = None,
                  logger: Any = None, metrics: Any = None):
    """Decorate a Lambda entry point; usable bare (`@event_context`) or with arguments."""
    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(event, context, *args, **kwargs):
            start_event_invocation(event, service=service, logger=logger, metrics=metrics)
            try:
                return func(event, context, *args, **kwargs)
            finally:
                end_event_invocation()
        return wrapper

    if handler is not None:
        return decorate(handler)
    return decorate
