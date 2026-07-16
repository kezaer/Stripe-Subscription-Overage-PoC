from __future__ import annotations

import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from streaming_billing.state import Catalog
from streaming_billing.usage import (
    UsageLedger,
    UsageUpdate,
    meter_error_details,
    reconcile_usage,
    report_usage,
    usage_update_from_subscription,
)
from streaming_billing.validation import InputError


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class MeterEvents:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    def create(self, params: dict, options: dict):
        self.calls.append((params, options))
        if self.fail:
            raise RuntimeError("simulated lost response")
        return SimpleNamespace(identifier=params["identifier"], livemode=False)


def client_for(service: MeterEvents):
    return SimpleNamespace(v1=SimpleNamespace(billing=SimpleNamespace(meter_events=service)))


def update(
    *,
    revision: int = 1,
    event_id: str = "customer-july-r1",
    total: str = "111",
    period_start: int = 1_800_000_005,
    period_end: int = 1_800_003_605,
):
    return UsageUpdate(
        "sub_test_subscription",
        "si_test_metered",
        "cus_test_customer",
        period_start,
        period_end,
        revision,
        event_id,
        Decimal(total),
    )


def _process_claim(path: str, start, results) -> None:
    start.wait()
    state, _ = UsageLedger(Path(path), clock=lambda: 1_800_000_120).claim_submission(update())
    results.put(state)


def sample_catalog() -> Catalog:
    return Catalog(
        "prod_a",
        "price_a",
        "prod_b",
        "price_b",
        "prod_o",
        "mtr_test",
        "price_overage",
        "coupon_test",
        "promo_test",
    )


def test_usage_persists_identifier_and_deduplicates_retry(tmp_path) -> None:
    clock = Clock(1_800_000_120)
    service = MeterEvents()
    ledger = UsageLedger(tmp_path, clock=clock)
    assert report_usage(client_for(service), ledger, update())["status"] == "submitted"
    assert report_usage(client_for(service), ledger, update())["status"] == "already_submitted"
    assert len(service.calls) == 1 and "timestamp" not in service.calls[0][0]


def test_lost_response_releases_lease_and_immediately_reuses_identity(tmp_path) -> None:
    clock = Clock(1_800_000_120)
    ledger = UsageLedger(tmp_path, lease_seconds=10, clock=clock)
    with pytest.raises(RuntimeError, match="lost response"):
        report_usage(client_for(MeterEvents(fail=True)), ledger, update())
    assert report_usage(client_for(MeterEvents()), ledger, update())["status"] == "submitted"
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT status FROM updates").fetchone()[0] == "submitted"
    assert not list(tmp_path.glob("*.tmp"))


def test_usage_rejects_stale_changed_and_reused_ids(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, clock=Clock(1_800_000_120))
    service = MeterEvents()
    report_usage(client_for(service), ledger, update(revision=2, event_id="r2"))
    with pytest.raises(InputError, match="older"):
        report_usage(client_for(service), ledger, update(revision=1, event_id="r1"))
    with pytest.raises(InputError, match="different usage data"):
        report_usage(client_for(service), ledger, update(revision=2, event_id="changed"))
    with pytest.raises(InputError, match="different usage data"):
        report_usage(client_for(service), ledger, update(revision=3, event_id="r2"))


def test_period_submission_lease_serializes_concurrent_revisions(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, clock=Clock(1_800_000_120))
    low = update(revision=1, event_id="r1")
    high = update(revision=2, event_id="r2")
    state, token = ledger.claim_submission(low)
    assert state == "claimed" and token
    assert ledger.claim_submission(high) == ("in_flight", None)
    ledger.mark_submitted(low, token or "")
    assert ledger.claim_submission(high)[0] == "claimed"


def test_concurrent_same_revision_has_one_network_owner(tmp_path) -> None:
    clock = Clock(1_800_000_120)
    barrier_ledger = UsageLedger(tmp_path, clock=clock)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: barrier_ledger.claim_submission(update()), range(16)))
    assert sum(state == "claimed" for state, _ in results) == 1
    assert sum(state == "in_flight" for state, _ in results) == 15


def test_two_processes_cannot_claim_same_revision(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_process_claim, args=(str(tmp_path), start, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    states = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(states) == ["claimed", "in_flight"]


def test_different_periods_can_be_claimed_independently(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, clock=Clock(1_800_000_120))
    other = update(event_id="other", period_start=1_800_003_605, period_end=1_800_007_205)
    assert ledger.claim_submission(update())[0] == "claimed"
    assert ledger.claim_submission(other)[0] == "claimed"


def test_subscription_supplies_customer_and_billing_period() -> None:
    item = SimpleNamespace(
        id="si_test_item",
        current_period_start=100,
        current_period_end=200,
        price=SimpleNamespace(
            id="price_overage", recurring=SimpleNamespace(usage_type="metered", meter="mtr_test")
        ),
    )
    subscription = SimpleNamespace(
        id="sub_test_sub",
        livemode=False,
        status="active",
        customer="cus_test_customer",
        items=SimpleNamespace(data=[item]),
    )
    client = SimpleNamespace(
        v1=SimpleNamespace(subscriptions=SimpleNamespace(retrieve=lambda *args: subscription))
    )
    result = usage_update_from_subscription(
        client, sample_catalog(), "sub_test_sub", 1, "stable-r1", Decimal("101")
    )
    assert result.customer_id == "cus_test_customer"
    assert (result.period_start, result.period_end) == (100, 200)


def test_thin_error_mapping_is_per_identifier_and_uses_error_type_code() -> None:
    event = {
        "data": {
            "reason": {
                "error_types": [
                    {
                        "code": "invalid_payload",
                        "sample_errors": [
                            {"error_message": "first", "request": {"identifier": "id-one"}},
                            {"error_message": "second", "request": {"identifier": "id-two"}},
                        ],
                    },
                    {
                        "code": "no_customer",
                        "sample_errors": [
                            {"error_message": "third", "request": {"identifier": "id-one"}}
                        ],
                    },
                ]
            }
        }
    }
    mapped = meter_error_details(event)
    assert mapped == {
        "id-one": [
            {"code": "invalid_payload", "message": "first"},
            {"code": "no_customer", "message": "third"},
        ],
        "id-two": [{"code": "invalid_payload", "message": "second"}],
    }


class Summaries:
    def __init__(self, value="11.0") -> None:
        self.value = value
        self.calls = []

    def list(self, meter_id, params):
        self.calls.append((meter_id, params))
        return SimpleNamespace(data=[SimpleNamespace(aggregated_value=self.value)])


def summary_client(summaries):
    return SimpleNamespace(
        v1=SimpleNamespace(
            billing=SimpleNamespace(meters=SimpleNamespace(event_summaries=summaries))
        )
    )


def test_reconciliation_uses_submission_minute_bounds_and_decimal_equality(tmp_path) -> None:
    clock = Clock(1_800_000_125)
    ledger = UsageLedger(tmp_path, clock=clock)
    report_usage(client_for(MeterEvents()), ledger, update())
    clock.value = 1_800_000_240
    summaries = Summaries("11.0")
    result = reconcile_usage(
        summary_client(summaries), sample_catalog(), ledger, "sub_test_subscription", clock=clock
    )
    assert result["status"] == "confirmed" and result["scope"] == "period_aggregate"
    params = summaries.calls[0][1]
    assert params["start_time"] == 1_800_000_120
    assert params["end_time"] == 1_800_000_240


def test_reconciliation_does_not_query_current_minute_or_out_of_window_receipt(tmp_path) -> None:
    clock = Clock(1_800_000_061)
    ledger = UsageLedger(tmp_path, clock=clock)
    report_usage(client_for(MeterEvents()), ledger, update())
    summaries = Summaries()
    result = reconcile_usage(
        summary_client(summaries), sample_catalog(), ledger, "sub_test_subscription", clock=clock
    )
    assert result["status"] == "unavailable" and not summaries.calls

    later_clock = Clock(1_800_004_000)
    future_ledger = UsageLedger(tmp_path / "future", clock=later_clock)
    report_usage(client_for(MeterEvents()), future_ledger, update())
    result = reconcile_usage(
        summary_client(summaries),
        sample_catalog(),
        future_ledger,
        "sub_test_subscription",
        clock=later_clock,
    )
    assert result["status"] == "unavailable"
    assert not summaries.calls
