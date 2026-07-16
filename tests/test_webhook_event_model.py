from __future__ import annotations

import json
import sqlite3

import pytest

from streaming_billing.webhooks import (
    HANDLED_EVENT_TYPES,
    THIN_EVENT_TYPES,
    LifecycleStore,
    LifecycleUpdate,
    WebhookEventStore,
    event_view,
)


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def update(
    event_id: str,
    event_type: str,
    created: int,
    *,
    subscription_id: str = "sub_test_portal",
) -> LifecycleUpdate:
    common = {
        "event_id": event_id,
        "event_type": event_type,
        "created": created,
        "subscription_id": subscription_id,
        "customer_id": "cus_test_portal",
    }
    if event_type == "checkout.session.completed":
        return LifecycleUpdate(
            **common,
            resource_id="cs_test_portal",
            checkout_session_id="cs_test_portal",
            access_state="awaiting_invoice",
        )
    if event_type == "invoice.paid":
        return LifecycleUpdate(
            **common,
            resource_id=f"in_test_{event_id}",
            invoice_id=f"in_test_{event_id}",
            invoice_status="paid",
            amount_due=1099,
            amount_paid=1099,
            currency="usd",
            access_state="active",
        )
    if event_type == "invoice.payment_failed":
        return LifecycleUpdate(
            **common,
            resource_id=f"in_test_{event_id}",
            invoice_id=f"in_test_{event_id}",
            invoice_status="open",
            amount_due=1099,
            amount_paid=0,
            currency="usd",
            access_state="payment_issue",
        )
    deleted = event_type == "customer.subscription.deleted"
    return LifecycleUpdate(
        **common,
        resource_id=subscription_id,
        subscription_status="canceled" if deleted else "active",
        access_state="inactive" if deleted else "active",
        cancel_at_period_end=False,
        current_period_end=200,
    )


@pytest.mark.parametrize("delivery_order", [("paid", "checkout"), ("checkout", "paid")])
def test_checkout_never_regresses_a_payment_outcome_across_different_seconds(
    tmp_path, delivery_order
) -> None:
    events = {
        "paid": update("evt_test_paid", "invoice.paid", 39),
        "checkout": update("evt_test_checkout", "checkout.session.completed", 40),
    }
    store = LifecycleStore(tmp_path)
    for name in delivery_order:
        store.apply(events[name])

    subscription = store.subscriptions()[0]
    assert subscription["access_state"] == "active"
    assert subscription["access_event_type"] == "invoice.paid"
    assert subscription["latest_invoice_status"] == "paid"


@pytest.mark.parametrize("delivery_order", [("deleted", "paid"), ("paid", "deleted")])
def test_subscription_deletion_is_terminal_against_later_invoice_events(
    tmp_path, delivery_order
) -> None:
    events = {
        "deleted": update("evt_test_deleted", "customer.subscription.deleted", 50),
        "paid": update("evt_test_paid_after_delete", "invoice.paid", 60),
    }
    store = LifecycleStore(tmp_path)
    for name in delivery_order:
        store.apply(events[name])

    subscription = store.subscriptions()[0]
    assert subscription["subscription_status"] == "canceled"
    assert subscription["access_state"] == "inactive"
    assert subscription["access_event_type"] == "customer.subscription.deleted"
    assert subscription["latest_invoice_status"] == "paid"


def test_projection_tracks_multiple_subscription_ids_independently(tmp_path) -> None:
    store = LifecycleStore(tmp_path)
    store.apply(
        update(
            "evt_test_checkout_one",
            "checkout.session.completed",
            10,
            subscription_id="sub_test_one",
        )
    )
    store.apply(update("evt_test_paid_two", "invoice.paid", 11, subscription_id="sub_test_two"))

    subscriptions = {row["subscription_id"]: row for row in store.subscriptions()}
    assert subscriptions["sub_test_one"]["access_state"] == "awaiting_invoice"
    assert subscriptions["sub_test_two"]["access_state"] == "active"


def test_existing_projection_is_rebuilt_from_allowlisted_history_on_upgrade(tmp_path) -> None:
    path = tmp_path / "subscription-lifecycle.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE lifecycle_events (
            event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, created INTEGER NOT NULL,
            resource_id TEXT, subscription_id TEXT, customer_id TEXT,
            subscription_status TEXT, access_state TEXT, checkout_session_id TEXT,
            invoice_id TEXT, invoice_status TEXT, amount_due INTEGER, amount_paid INTEGER,
            currency TEXT, cancel_at_period_end INTEGER, current_period_end INTEGER
            )"""
        )
        db.execute(
            """CREATE TABLE subscriptions (
            subscription_id TEXT PRIMARY KEY, customer_id TEXT, subscription_status TEXT,
            access_state TEXT NOT NULL, checkout_session_id TEXT, latest_invoice_id TEXT,
            latest_invoice_status TEXT, latest_amount_due INTEGER,
            latest_amount_paid INTEGER, currency TEXT, cancel_at_period_end INTEGER,
            current_period_end INTEGER, last_event_id TEXT NOT NULL,
            last_event_type TEXT NOT NULL, last_event_created INTEGER NOT NULL
            )"""
        )
        db.execute(
            "INSERT INTO lifecycle_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "evt_test_paid_old",
                "invoice.paid",
                39,
                "in_test_old",
                "sub_test_old",
                "cus_test_old",
                None,
                "active",
                None,
                "in_test_old",
                "paid",
                879,
                879,
                "usd",
                None,
                None,
            ),
        )
        db.execute(
            "INSERT INTO lifecycle_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "evt_test_checkout_old",
                "checkout.session.completed",
                40,
                "cs_test_old",
                "sub_test_old",
                "cus_test_old",
                None,
                "awaiting_invoice",
                "cs_test_old",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )
        db.execute(
            "INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sub_test_old",
                "cus_test_old",
                None,
                "awaiting_invoice",
                "cs_test_old",
                "in_test_old",
                "paid",
                879,
                879,
                "usd",
                None,
                None,
                "evt_test_checkout_old",
                "checkout.session.completed",
                40,
            ),
        )

    repaired = LifecycleStore(tmp_path).subscriptions()[0]
    assert repaired["access_state"] == "active"
    assert repaired["access_event_type"] == "invoice.paid"
    assert repaired["latest_invoice_id"] == "in_test_old"


@pytest.mark.parametrize("event_type", sorted(HANDLED_EVENT_TYPES | THIN_EVENT_TYPES))
def test_every_supported_event_has_readable_copy(event_type) -> None:
    view = event_view(
        {
            "id": "evt_test_copy",
            "type": event_type,
            "created": 100,
            "resource_id": None,
            "handled": True,
            "status": "processed",
            "processed_at": 101,
            "received_at": 100,
            "completed_at": 101,
            "details": {},
        }
    )
    assert view["title"]
    assert view["explanation"]
    assert view["application_action"]
    assert view["entitlement_impact"]
    assert view["next_step"]
    assert view["verification_status"] == "Verified"
    assert view["processing_status"] == "Processed"
    assert view["created_iso"] == "1970-01-01T00:01:40Z"
    assert view["processed_iso"] == "1970-01-01T00:01:41Z"


def test_event_store_returns_relations_two_times_and_allowlisted_result(tmp_path) -> None:
    clock = Clock(1_000)
    store = WebhookEventStore(tmp_path, clock=clock)
    token = store.claim_with_token(
        {
            "id": "evt_test_feed",
            "type": "invoice.paid",
            "created": 900,
            "resource_id": "in_test_feed",
            "handled": True,
            "subscription_id": "sub_test_feed",
            "customer_id": "cus_test_feed",
            "subscription_status": None,
            "access_state": "active",
            "invoice_status": "paid",
        }
    )
    clock.value = 1_005
    assert token is not None
    assert store.complete(
        "evt_test_feed",
        {
            "lifecycle_updated": True,
            "subscription_id": "sub_test_feed",
            "email": "must-not-be-saved@example.com",
            "raw_payload": {"secret": "must-not-be-saved"},
        },
        claim_token=token,
    )

    view = store.recent()[0]
    assert view["received_at"] == 1_000
    assert view["processed_at"] == 1_005
    assert view["received_iso"] == "1970-01-01T00:16:40Z"
    assert view["processed_iso"] == "1970-01-01T00:16:45Z"
    assert view["details"] == {
        "lifecycle_updated": True,
        "subscription_id": "sub_test_feed",
    }
    assert view["object_relations"] == [
        {"object_type": "Invoice", "id": "in_test_feed"},
        {"object_type": "Subscription", "id": "sub_test_feed"},
        {"object_type": "Customer", "id": "cus_test_feed"},
    ]
    assert "must-not-be-saved" not in json.dumps(view)


def test_verified_unsupported_snapshot_has_safe_fallback_copy(tmp_path) -> None:
    store = WebhookEventStore(tmp_path)
    assert store.record(
        {
            "id": "evt_test_unhandled",
            "type": "product.updated",
            "created": 100,
            "resource_id": "prod_test_unhandled",
            "handled": False,
        }
    )

    view = store.recent()[0]
    assert view["title"] == "product.updated"
    assert view["application_action"] == (
        "No application action is configured for this event type."
    )
    assert view["entitlement_impact"] == "No subscription access change."
