import json
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools.utilities.data_classes import EventBridgeEvent, event_source

from lambda_app_common.config import configure
from lambda_app_common.events import classify_event, current_invocation, event_context

from .helpers import lambda_context


def eventbridge(detail=None, source="documents.process"):
    return {"version": "0", "id": "e-1", "source": source, "detail-type": "DocumentReady",
            "account": "000000000000", "time": "2026-10-04T00:00:00Z", "region": "us-east-1",
            "resources": [], "detail": detail if detail is not None else {"organization": "ACME", "username": "jane"}}


def sqs(body):
    return {"Records": [{"messageId": "m-1", "eventSource": "aws:sqs", "eventSourceARN": "arn:queue",
                         "body": json.dumps(body) if not isinstance(body, str) else body}]}


def ses(sender="Billing.Team+x@acme.example"):
    return {"Records": [{"eventSource": "aws:ses", "ses": {"mail": {
        "messageId": "ses-1", "source": sender, "destination": ["in@example.com"],
        "commonHeaders": {"subject": "Invoice"}}}}]}


def test_eventbridge_events_are_classified_with_their_identity():
    invocation = classify_event(eventbridge())
    assert invocation.kind == "eventbridge"
    assert (invocation.source, invocation.detail_type) == ("documents.process", "DocumentReady")
    assert (invocation.organization, invocation.username) == ("ACME", "jane")


def test_a_powertools_data_class_is_unwrapped():
    invocation = classify_event(EventBridgeEvent(eventbridge()))
    assert invocation.kind == "eventbridge"
    assert invocation.organization == "ACME"


def test_sqs_reads_identity_from_the_first_body():
    invocation = classify_event(sqs({"organization": "ACME", "username": "bot"}))
    assert invocation.kind == "sqs"
    assert (invocation.organization, invocation.message_id) == ("ACME", "m-1")


def test_an_sqs_body_that_is_not_json_does_not_raise():
    assert classify_event(sqs("not json")).organization is None


def test_ses_attributes_mail_to_the_sender_local_part():
    invocation = classify_event(ses())
    assert invocation.kind == "ses"
    assert invocation.organization == "billingteamx"
    assert invocation.ses_subject == "Invoice"


def test_anything_else_is_unknown_rather_than_an_error():
    assert classify_event({"foo": "bar"}).kind == "unknown"
    assert classify_event("not a dict").kind == "unknown"


def test_the_decorator_runs_hooks_with_the_raw_dict_before_the_handler():
    order = []
    configure(stage="dev", application="App", on_invocation_start=[lambda e: order.append(("hook", type(e)))])

    @event_context(service="Docs")
    @event_source(data_class=EventBridgeEvent)
    def handler(event, context):
        order.append(("handler", current_invocation().organization))
        return "ok"

    assert handler(eventbridge(), lambda_context()) == "ok"
    assert order == [("hook", dict), ("handler", "ACME")]
    assert current_invocation() is None


def test_the_decorator_appends_bounded_logger_keys_and_optionally_records_the_metric():
    logger, metrics = MagicMock(), MagicMock()
    configure(stage="tech", application="App")

    @event_context(service="Docs", logger=logger, metrics=metrics)
    def handler(event, context):
        return None

    handler(eventbridge({"organization": "ACME", "blob": "x" * 10_000}), lambda_context())

    keys = logger.append_keys.call_args.kwargs
    assert keys["env"] == "tech" and keys["application"] == "App"
    assert keys["detail"]["truncated"] is True
    assert metrics.add_metric.call_args.kwargs["name"] == "api_request"


def test_the_bare_decorator_form_works():
    @event_context
    def handler(event, context):
        return current_invocation().kind

    assert handler(sqs({}), lambda_context()) == "sqs"


def test_the_invocation_is_cleared_even_when_the_handler_raises():
    @event_context
    def handler(event, context):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        handler(eventbridge(), lambda_context())
    assert current_invocation() is None


def test_a_failing_start_hook_fails_the_invocation():
    def boom(_):
        raise RuntimeError("could not reset")

    configure(on_invocation_start=[boom])
    handler = event_context(lambda e, c: "ran")
    with pytest.raises(RuntimeError):
        handler(eventbridge(), lambda_context())


def test_warmup_events_are_recognisable():
    assert classify_event(eventbridge(source="serverless.warmup")).is_warmup is True
    assert classify_event(eventbridge()).is_warmup is False


def test_large_events_are_bounded_in_the_debug_dump(capsys, monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    handler = event_context(lambda e, c: None, service="Docs")
    handler(eventbridge({"blob": "x" * 50_000}), lambda_context())
    out = capsys.readouterr().out
    assert "[EVENT] Docs | documents.process -> DocumentReady" in out
    assert len(out) < 5_000
