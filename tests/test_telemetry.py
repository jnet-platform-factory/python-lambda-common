"""Telemetry is bounded, JSON-safe, and never the reason an invocation fails."""

import json
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from aws_lambda_powertools import Metrics

from lambda_app_common import telemetry
from lambda_app_common.events.middleware import classify_event, event_logger_metadata
from lambda_app_common.telemetry import DETAIL_LOG_MAX_CHARS, _bounded_detail, bounded_detail, emf_safe


def eventbridge(detail):
    return {"source": "catalog.import_records", "detail-type": "CatalogImportNewParts", "detail": detail}


def metadata_for(detail):
    return event_logger_metadata(classify_event(eventbridge(detail)), "cloud", "Documents")


def big_detail(parts=2000):
    return {
        "organization": "ACME",
        "catalog_part_ids": [f"CP-{i}" for i in range(parts)],
        "catalog_parts_data": [
            {"catalog_part_id": f"CP-{i}", "seller_part_no": f"PART-{i}",
             "invoice_description": "METAL BRACKET FOR ENGINE", "hts_code": "84099999"}
            for i in range(parts)
        ],
    }


# --- bounded detail: a persistent logger key is repeated on every line --------------------

def test_the_limit_is_unchanged():
    assert DETAIL_LOG_MAX_CHARS == 2048
    assert _bounded_detail is bounded_detail


def test_an_ordinary_detail_is_still_attached_in_full():
    detail = {"document_id": "doc-1", "batch_id": "Batch-1"}
    assert metadata_for(detail)["detail"] == detail


def test_a_huge_detail_is_replaced_by_its_shape():
    detail = big_detail()
    attached = metadata_for(detail)["detail"]

    assert attached["truncated"] is True
    assert attached["size_chars"] == len(json.dumps(detail))
    assert attached["keys"] == ["catalog_part_ids", "catalog_parts_data", "organization"]


def test_the_attached_key_stays_small_however_big_the_event_is():
    for parts in (2_000, 20_000):
        assert len(json.dumps(metadata_for(big_detail(parts))["detail"])) < 4096


def test_the_correlation_keys_survive_truncation():
    metadata = metadata_for(big_detail())
    assert metadata["source"] == "catalog.import_records"
    assert metadata["detail_type"] == "CatalogImportNewParts"
    assert metadata["organization"] == "ACME"


def test_a_detail_that_will_not_serialise_does_not_raise():
    circular = {"self": None}
    circular["self"] = circular
    assert bounded_detail(circular) == {"truncated": True, "reason": "not JSON-serialisable"}


def test_a_missing_detail_is_left_alone():
    assert bounded_detail(None) is None


def test_a_non_dict_detail_is_summarised_without_keys():
    attached = bounded_detail(["x" * 100] * 200)
    assert attached["truncated"] is True
    assert attached["keys"] is None


# --- emf_safe: metadata must survive the json.dumps at flush time -------------------------

def test_primitives_are_left_exactly_as_they_are():
    for value in ("text", 7, 1.5, True, None):
        assert emf_safe(value) is value


def test_a_date_in_the_detail_no_longer_kills_the_flush():
    detail = {"document_date": date(2026, 9, 19), "created_at": datetime(2026, 9, 19, 20, 45, 6),
              "amount": Decimal("56.00"), "document_id": "doc-1"}
    safe = emf_safe(detail)
    json.dumps(safe)
    assert safe["document_date"] == "2026-09-19"
    assert safe["amount"] == "56.00"
    assert safe["document_id"] == "doc-1"


def test_it_reaches_into_nested_structures():
    assert emf_safe({"documents": [{"seen_on": date(2026, 9, 19)}], "pages": (1, 2)}) == \
        {"documents": [{"seen_on": "2026-09-19"}], "pages": [1, 2]}


def test_an_orm_object_becomes_a_string_rather_than_an_exception():
    class Reference:
        def __repr__(self):
            return "<Reference id=1>"

    assert emf_safe({"reference": Reference()}) == {"reference": "<Reference id=1>"}


def test_deep_and_self_referencing_structures_terminate():
    payload = {"name": "loop"}
    payload["self"] = payload
    json.dumps(emf_safe(payload))

    deep = node = {}
    for _ in range(50):
        node["next"] = {}
        node = node["next"]
    json.dumps(emf_safe(deep))


def test_non_string_keys_survive_as_strings():
    assert emf_safe({1: "one", date(2026, 9, 19): "day"}) == {"1": "one", "2026-09-19": "day"}


# --- the api_request metric ---------------------------------------------------------------

def test_record_api_request_sanitizes_before_handing_anything_to_powertools():
    metrics = MagicMock()
    telemetry.record_api_request(metrics, {"env": "tech", "read_only": True},
                                 {"detail": {"document_date": date(2026, 9, 19)}, "organization": "ACME"})

    passed = {c.kwargs["key"]: c.kwargs["value"] for c in metrics.add_metadata.call_args_list}
    json.dumps(passed)
    assert passed["detail"] == {"document_date": "2026-09-19"}
    dims = {c.kwargs["name"]: c.kwargs["value"] for c in metrics.add_dimension.call_args_list}
    assert dims == {"env": "tech", "read_only": "True"}
    assert metrics.add_metric.call_args.kwargs["name"] == "api_request"


def test_record_api_request_flushes_through_real_powertools_metrics(capsys):
    metrics = Metrics(namespace="Test", service="svc")
    telemetry.record_api_request(metrics, telemetry.metrics_dimensions("dev", "ACME", "GET"),
                                 {"when": date(2026, 9, 19)})
    metrics.flush_metrics()

    emitted = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert emitted["api_request"] == [1.0] or emitted["api_request"] == 1.0
    assert emitted["organization"] == "ACME"
    assert emitted["read_only"] == "True"


def test_a_broken_metrics_object_never_raises():
    metrics = MagicMock()
    metrics.add_dimension.side_effect = RuntimeError("boom")
    telemetry.record_api_request(metrics, {"env": "dev"}, {})


def test_a_logger_without_append_keys_is_ignored():
    import logging
    telemetry.append_logger_keys(logging, {"a": 1})
    telemetry.append_logger_keys(None, {"a": 1})
