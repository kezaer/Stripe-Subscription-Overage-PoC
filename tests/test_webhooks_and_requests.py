from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from streaming_billing.config import STRIPE_API_VERSION, Settings
from streaming_billing.state import CATALOG_FIELDS, CatalogStore
from streaming_billing.web import create_app
from streaming_billing.webhooks import (
    LifecycleStore,
    LifecycleUpdate,
    WebhookEventStore,
    event_summary,
)


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class CheckoutSessions:
    def __init__(self, returned_plan: str = "b") -> None:
        self.created: list[tuple[dict, dict]] = []
        self.returned_plan = returned_plan

    def create(self, params: dict, options: dict):
        self.created.append((params, options))
        return SimpleNamespace(
            id="cs_test_created",
            livemode=False,
            url="https://checkout.stripe.com/c/pay/test",
        )

    def retrieve(self, session_id: str, params: dict):
        return SimpleNamespace(
            id=session_id,
            livemode=False,
            customer="cus_test_customer",
            subscription=SimpleNamespace(
                id="sub_test_subscription",
                metadata={"streaming_plan": self.returned_plan},
            ),
            metadata={"streaming_plan": self.returned_plan},
            status="complete",
            payment_status="paid",
        )


def settings(tmp_path) -> Settings:
    return Settings(
        stripe_secret_key="sk_test_" + "A" * 24,
        webhook_secret="whsec_" + "B" * 24,
        base_url="http://127.0.0.1:8000",
        state_dir=tmp_path,
        thin_webhook_secret="whsec_" + "C" * 24,
    )


def signed_header(payload: bytes, secret: str) -> str:
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + payload
    signature = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def request(app, method: str, path: str, **kwargs):
    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def complete_catalog(tmp_path) -> None:
    store = CatalogStore(tmp_path)
    for name, prefix in CATALOG_FIELDS.items():
        store.save_resource(name, f"{prefix or 'coupon_'}test_{name}")


def snapshot_payload(event_id: str, event_type: str, created: int, resource: dict) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "api_version": STRIPE_API_VERSION,
            "created": created,
            "livemode": False,
            "type": event_type,
            "data": {"object": resource},
        },
        separators=(",", ":"),
    ).encode()


def test_webhook_summary_rejects_live_events() -> None:
    event = SimpleNamespace(
        id="evt_live_event",
        type="invoice.paid",
        created=int(time.time()),
        livemode=True,
        data=SimpleNamespace(object=SimpleNamespace(id="in_live_invoice")),
    )
    with pytest.raises(ValueError, match="test mode"):
        event_summary(event)


def test_observed_event_summary_keeps_safe_customer_reference() -> None:
    summary = event_summary(
        SimpleNamespace(
            id="evt_test_observed_customer",
            type="payment_intent.succeeded",
            created=100,
            livemode=False,
            data=SimpleNamespace(
                object=SimpleNamespace(
                    id="pi_test_observed_customer",
                    customer="cus_test_observed_customer",
                )
            ),
        )
    )

    assert summary["handled"] is False
    assert summary["customer_id"] == "cus_test_observed_customer"
    assert summary["subscription_id"] is None


def test_webhook_uses_raw_signed_body_and_deduplicates(tmp_path) -> None:
    local_settings = settings(tmp_path)
    checkout = CheckoutSessions()
    app = create_app(
        local_settings,
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=checkout))),
    )
    payload = json.dumps(
        {
            "id": "evt_test_webhook",
            "object": "event",
            "api_version": STRIPE_API_VERSION,
            "created": int(time.time()),
            "livemode": False,
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_test_created", "object": "checkout.session"}},
        },
        separators=(",", ":"),
    ).encode()
    header = signed_header(payload, local_settings.webhook_secret or "")

    first = request(
        app,
        "POST",
        "/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )
    second = request(
        app,
        "POST",
        "/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )

    assert first.status_code == 200
    assert first.json() == {"received": True, "duplicate": False}
    assert second.json() == {"received": True, "duplicate": True}


def test_webhook_store_claim_is_concurrency_safe(tmp_path) -> None:
    store = WebhookEventStore(tmp_path)
    summary = {
        "id": "evt_test_concurrent",
        "type": "invoice.paid",
        "created": 1,
        "resource_id": "in_test",
        "handled": True,
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: store.claim(summary), range(24)))
    assert claimed.count(True) == 1


def test_webhook_claim_survives_restart_and_recovers_only_after_lease(tmp_path) -> None:
    clock = Clock(1_000)
    summary = {
        "id": "evt_test_restart",
        "type": "invoice.paid",
        "created": 1,
        "resource_id": "in_test",
        "handled": True,
    }
    assert WebhookEventStore(tmp_path, lease_seconds=10, clock=clock).claim(summary)
    restarted = WebhookEventStore(tmp_path, lease_seconds=10, clock=clock)
    assert not restarted.claim(summary)
    clock.value = 1_011
    assert restarted.claim(summary)
    restarted.complete(summary["id"])
    clock.value = 2_000
    assert not WebhookEventStore(tmp_path, lease_seconds=10, clock=clock).claim(summary)


def test_expired_webhook_worker_cannot_complete_new_owners_lease(tmp_path) -> None:
    clock = Clock(1_000)
    store = WebhookEventStore(tmp_path, lease_seconds=10, clock=clock)
    summary = {
        "id": "evt_test_lease_owner",
        "type": "invoice.paid",
        "created": 1,
        "resource_id": "in_test",
        "handled": True,
    }
    first_token = store.claim_with_token(summary)
    assert first_token
    clock.value = 1_011
    second_token = store.claim_with_token(summary)
    assert second_token and second_token != first_token
    assert not store.complete(summary["id"], claim_token=first_token)
    assert store.complete(summary["id"], claim_token=second_token)


def test_snapshot_webhooks_build_allowlisted_lifecycle_projection(tmp_path) -> None:
    local_settings = settings(tmp_path)
    app = create_app(
        local_settings,
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=CheckoutSessions()))),
    )
    now = int(time.time())
    events = [
        (
            "evt_test_subscription_created",
            "customer.subscription.created",
            {
                "id": "sub_test_lifecycle",
                "customer": "cus_test_lifecycle",
                "status": "active",
                "cancel_at_period_end": False,
                "current_period_end": now + 3600,
            },
        ),
        (
            "evt_test_checkout_lifecycle",
            "checkout.session.completed",
            {
                "id": "cs_test_lifecycle",
                "customer": "cus_test_lifecycle",
                "subscription": "sub_test_lifecycle",
                "customer_details": {"email": "must-not-be-saved@example.com"},
            },
        ),
        (
            "evt_test_invoice_paid",
            "invoice.paid",
            {
                "id": "in_test_paid",
                "customer": "cus_test_lifecycle",
                "parent": {"subscription_details": {"subscription": "sub_test_lifecycle"}},
                "status": "paid",
                "amount_due": 1099,
                "amount_paid": 1099,
                "currency": "usd",
                "description": "must not be saved",
            },
        ),
        (
            "evt_test_invoice_failed",
            "invoice.payment_failed",
            {
                "id": "in_test_failed",
                "customer": "cus_test_lifecycle",
                "subscription": "sub_test_lifecycle",
                "status": "open",
                "amount_due": 1299,
                "amount_paid": 0,
                "currency": "usd",
            },
        ),
        (
            "evt_test_subscription_updated",
            "customer.subscription.updated",
            {
                "id": "sub_test_lifecycle",
                "customer": "cus_test_lifecycle",
                "status": "past_due",
                "cancel_at_period_end": True,
                "current_period_end": now + 3600,
            },
        ),
        (
            "evt_test_subscription_deleted",
            "customer.subscription.deleted",
            {
                "id": "sub_test_lifecycle",
                "customer": "cus_test_lifecycle",
                "status": "canceled",
                "cancel_at_period_end": False,
                "current_period_end": now + 3600,
            },
        ),
    ]
    for offset, (event_id, event_type, resource) in enumerate(events):
        payload = snapshot_payload(event_id, event_type, now + offset, resource)
        response = request(
            app,
            "POST",
            "/webhooks/stripe",
            content=payload,
            headers={
                "Stripe-Signature": signed_header(payload, local_settings.webhook_secret or "")
            },
        )
        assert response.status_code == 200
        assert response.json() == {"received": True, "duplicate": False}

    store = LifecycleStore(tmp_path)
    subscription = store.subscriptions()[0]
    assert subscription["subscription_id"] == "sub_test_lifecycle"
    assert subscription["customer_id"] == "cus_test_lifecycle"
    assert subscription["subscription_status"] == "canceled"
    assert subscription["access_state"] == "inactive"
    assert len(store.recent()) == 6
    saved = json.dumps(store.recent())
    assert "must-not-be-saved" not in saved

    home = request(app, "GET", "/")
    assert "sub_test_lifecycle" in home.text
    assert "Service access: Inactive" in home.text
    assert "customer.subscription.deleted" in home.text


def test_snapshot_webhook_records_observed_only_event_without_lifecycle_change(tmp_path) -> None:
    local_settings = settings(tmp_path)
    app = create_app(
        local_settings,
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=CheckoutSessions()))),
    )
    payload = snapshot_payload(
        "evt_test_observed_only",
        "product.updated",
        int(time.time()),
        {"id": "prod_test_observed_only", "name": "must-not-be-saved"},
    )

    response = request(
        app,
        "POST",
        "/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": signed_header(payload, local_settings.webhook_secret or "")},
    )

    assert response.status_code == 200
    assert response.json() == {"received": True, "duplicate": False}
    delivery = WebhookEventStore(tmp_path).recent()[0]
    assert delivery["event_type"] == "product.updated"
    assert delivery["handled"] is False
    assert LifecycleStore(tmp_path).subscriptions() == []
    home = request(app, "GET", "/")
    assert "product.updated" in home.text
    assert "Observed only" in home.text
    assert "must-not-be-saved" not in home.text


def test_subscription_created_records_status_without_granting_paid_access(tmp_path) -> None:
    local_settings = settings(tmp_path)
    app = create_app(
        local_settings,
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=CheckoutSessions()))),
    )
    payload = snapshot_payload(
        "evt_test_created_status_only",
        "customer.subscription.created",
        int(time.time()),
        {
            "id": "sub_test_created_status_only",
            "customer": "cus_test_created_status_only",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_end": int(time.time()) + 3600,
        },
    )

    response = request(
        app,
        "POST",
        "/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": signed_header(payload, local_settings.webhook_secret or "")},
    )

    assert response.status_code == 200
    subscription = LifecycleStore(tmp_path).subscriptions()[0]
    assert subscription["subscription_status"] == "active"
    assert subscription["access_state"] == "unknown"
    assert subscription["access_event_type"] is None


def test_home_explains_webhooks_in_receipt_order_for_multiple_subscriptions(tmp_path) -> None:
    clock = Clock(1_000)
    event_store = WebhookEventStore(tmp_path, clock=clock)
    assert event_store.record(
        {
            "id": "evt_test_paid_portal",
            "type": "invoice.paid",
            "created": 900,
            "resource_id": "in_test_paid_portal",
            "handled": True,
            "subscription_id": "sub_test_paid_portal",
            "customer_id": "cus_test_portal",
            "access_state": "active",
            "invoice_status": "paid",
        }
    )
    clock.value = 1_001
    assert event_store.record(
        {
            "id": "evt_test_checkout_portal",
            "type": "checkout.session.completed",
            "created": 901,
            "resource_id": "cs_test_checkout_portal",
            "handled": True,
            "subscription_id": "sub_test_checkout_portal",
            "customer_id": "cus_test_portal",
            "access_state": "awaiting_invoice",
        }
    )

    lifecycle_store = LifecycleStore(tmp_path)
    lifecycle_store.apply(
        LifecycleUpdate(
            event_id="evt_test_paid_portal",
            event_type="invoice.paid",
            created=900,
            resource_id="in_test_paid_portal",
            subscription_id="sub_test_paid_portal",
            customer_id="cus_test_portal",
            access_state="active",
            invoice_id="in_test_paid_portal",
            invoice_status="paid",
            amount_paid=2499,
            currency="usd",
        )
    )
    lifecycle_store.apply(
        LifecycleUpdate(
            event_id="evt_test_checkout_portal",
            event_type="checkout.session.completed",
            created=901,
            resource_id="cs_test_checkout_portal",
            subscription_id="sub_test_checkout_portal",
            customer_id="cus_test_portal",
            access_state="awaiting_invoice",
            checkout_session_id="cs_test_checkout_portal",
        )
    )

    app = create_app(
        settings(tmp_path),
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=CheckoutSessions()))),
    )
    home = request(app, "GET", "/")

    assert home.status_code == 200
    assert "Webhook deliveries" in home.text
    assert home.text.count('<details class="webhook-event">') == 2
    assert home.text.index("evt_test_checkout_portal") < home.text.index("evt_test_paid_portal")
    assert "2 verified deliveries · newest first" in home.text
    assert 'id="webhook-delivery-toggle"' not in home.text
    assert "sub_test_paid_portal" in home.text
    assert "sub_test_checkout_portal" in home.text
    assert "What Stripe reported" in home.text
    assert "What the PoC did" in home.text
    assert "Streaming access" in home.text
    assert "What you can do next" in home.text
    assert "Technical details" in home.text
    assert "Verified delivery ledger" not in home.text


def test_home_collapses_to_latest_five_without_omitting_deliveries_from_dom(tmp_path) -> None:
    clock = Clock(2_000)
    event_store = WebhookEventStore(tmp_path, clock=clock)
    for number in range(12):
        assert event_store.record(
            {
                "id": f"evt_test_feed_{number:02d}",
                "type": "product.updated",
                "created": 1_000 + number,
                "resource_id": f"prod_test_feed_{number:02d}",
                "handled": False,
            }
        )
        clock.value += 1

    app = create_app(
        settings(tmp_path),
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=CheckoutSessions()))),
    )
    home = request(app, "GET", "/")

    assert home.text.count('<details class="webhook-event">') == 12
    assert home.text.index("evt_test_feed_11") < home.text.index("evt_test_feed_00")
    assert 'data-collapsed-limit="5"' in home.text
    assert "Showing latest 5 of 12 verified deliveries" in home.text
    assert "Show all 12 deliveries" in home.text
    assert home.text.count('class="badge status-observed"') == 12


def test_lifecycle_projection_does_not_regress_on_same_timestamp(tmp_path) -> None:
    store = LifecycleStore(tmp_path)
    store.apply(
        LifecycleUpdate(
            event_id="evt_test_paid_same_second",
            event_type="invoice.paid",
            created=100,
            resource_id="in_test_same_second",
            subscription_id="sub_test_same_second",
            customer_id="cus_test_same_second",
            subscription_status="active",
            access_state="active",
            invoice_id="in_test_same_second",
            invoice_status="paid",
        )
    )
    store.apply(
        LifecycleUpdate(
            event_id="evt_test_checkout_same_second",
            event_type="checkout.session.completed",
            created=100,
            resource_id="cs_test_same_second",
            subscription_id="sub_test_same_second",
            customer_id="cus_test_same_second",
            access_state="awaiting_invoice",
            checkout_session_id="cs_test_same_second",
        )
    )

    subscription = store.subscriptions()[0]

    assert subscription["access_state"] == "active"
    assert subscription["last_event_type"] == "invoice.paid"
    assert subscription["latest_invoice_id"] == "in_test_same_second"


def test_webhook_rejects_bad_signature(tmp_path) -> None:
    local_settings = settings(tmp_path)
    app = create_app(
        local_settings,
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=CheckoutSessions()))),
    )
    response = request(
        app,
        "POST",
        "/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "t=1,v1=bad"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid Stripe webhook."}


def test_thin_meter_error_is_fetched_and_recorded(tmp_path) -> None:
    class Notification:
        id = "evt_test_thin_error"
        type = "v1.billing.meter.error_report_triggered"
        created = "2026-07-15T12:00:00.000Z"
        livemode = False

        def fetch_event(self):
            return {"data": {"reason": {"error_types": []}}}

    client = SimpleNamespace(
        parse_event_notification=lambda *args: Notification(),
        v1=SimpleNamespace(checkout=SimpleNamespace(sessions=CheckoutSessions())),
    )
    app = create_app(settings(tmp_path), client)
    response = request(
        app,
        "POST",
        "/webhooks/stripe/thin",
        content=b"{}",
        headers={"Stripe-Signature": "verified-by-fake"},
    )
    assert response.status_code == 200
    assert response.json() == {"received": True, "duplicate": False, "matched": 0}


def test_checkout_and_home_work_without_optional_webhook_secrets(tmp_path) -> None:
    complete_catalog(tmp_path)
    checkout = CheckoutSessions()
    local_settings = Settings(
        stripe_secret_key="sk_test_" + "A" * 24,
        webhook_secret=None,
        base_url="http://127.0.0.1:8000",
        state_dir=tmp_path,
        thin_webhook_secret=None,
    )
    app = create_app(
        local_settings,
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=checkout))),
    )
    home = request(app, "GET", "/")
    checkout_response = request(
        app,
        "POST",
        "/checkout",
        data={
            "plan": "a",
            "checkout_request_id": "7a68d606-2ee9-4f9e-a7fd-0f3625ef8824",
        },
        follow_redirects=False,
    )
    snapshot = request(app, "POST", "/webhooks/stripe", content=b"{}")
    thin = request(app, "POST", "/webhooks/stripe/thin", content=b"{}")

    assert home.status_code == 200
    assert "Webhooks disabled" in home.text
    assert checkout_response.status_code == 303
    assert snapshot.status_code == 503
    assert "Checkout and usage still work" in snapshot.json()["detail"]
    assert thin.status_code == 503


def test_browser_usage_submission_is_retry_safe_and_shows_thin_error(tmp_path) -> None:
    complete_catalog(tmp_path)
    catalog = CatalogStore(tmp_path).catalog()

    class MeterEvents:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        def create(self, params: dict, options: dict):
            self.calls.append((params, options))
            return SimpleNamespace(identifier=params["identifier"], livemode=False)

    class Notification:
        id = "evt_test_browser_meter_error"
        type = "v1.billing.meter.error_report_triggered"
        created = "2026-07-15T12:00:00.000Z"
        livemode = False

        def fetch_event(self):
            return {
                "data": {
                    "reason": {
                        "error_types": [
                            {
                                "code": "invalid_value",
                                "sample_errors": [
                                    {
                                        "error_message": "The usage value was rejected.",
                                        "request": {"identifier": meters.calls[0][0]["identifier"]},
                                    }
                                ],
                            }
                        ]
                    }
                }
            }

    item = SimpleNamespace(
        id="si_test_browser_metered",
        current_period_start=1_800_000_000,
        current_period_end=1_802_678_400,
        price=SimpleNamespace(
            id=catalog.plan_b_overage_price,
            recurring=SimpleNamespace(usage_type="metered", meter=catalog.plan_b_meter),
        ),
    )
    subscription = SimpleNamespace(
        id="sub_test_browser",
        livemode=False,
        status="active",
        customer="cus_test_browser",
        items=SimpleNamespace(data=[item]),
    )
    meters = MeterEvents()
    client = SimpleNamespace(
        parse_event_notification=lambda *args: Notification(),
        v1=SimpleNamespace(
            checkout=SimpleNamespace(sessions=CheckoutSessions()),
            subscriptions=SimpleNamespace(retrieve=lambda *args: subscription),
            billing=SimpleNamespace(meter_events=meters),
        ),
    )
    app = create_app(settings(tmp_path), client)
    form = {
        "subscription_id": "sub_test_browser",
        "total_gb": "111",
    }
    first = request(app, "POST", "/usage", data=form, follow_redirects=False)
    retry = request(app, "POST", "/usage", data=form, follow_redirects=False)
    result = request(app, "GET", first.headers["location"])

    assert first.status_code == 303 and retry.status_code == 303
    assert len(meters.calls) == 1
    assert meters.calls[0][0]["payload"]["value"] == "11"
    assert "Accepted by Stripe API" in result.text
    assert "$12.99" in result.text
    assert "not final invoice or aggregate confirmation" in result.text

    thin = request(
        app,
        "POST",
        "/webhooks/stripe/thin",
        content=b"{}",
        headers={"Stripe-Signature": "verified-by-fake"},
    )
    rejected = request(app, "GET", "/")
    assert thin.json() == {"received": True, "duplicate": False, "matched": 1}
    assert "Rejected asynchronously" in rejected.text
    assert "invalid_value" in rejected.text
    assert "The usage value was rejected." in rejected.text


def test_checkout_and_success_routes(tmp_path) -> None:
    complete_catalog(tmp_path)
    checkout = CheckoutSessions()
    app = create_app(
        settings(tmp_path),
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=checkout))),
    )
    checkout_response = request(
        app,
        "POST",
        "/checkout",
        data={
            "plan": "b",
            "checkout_request_id": "7a68d606-2ee9-4f9e-a7fd-0f3625ef8824",
        },
        follow_redirects=False,
    )
    success_response = request(app, "GET", "/success?session_id=cs_test_returned")

    assert checkout_response.status_code == 303
    assert checkout_response.headers["location"].startswith("https://checkout.stripe.com/")
    assert "quantity" not in checkout.created[0][0]["line_items"][1]
    assert success_response.status_code == 200
    assert "Plan B checkout complete" in success_response.text
    assert "cus_test_customer" in success_response.text
    assert "Use verified webhooks" in success_response.text


def test_plan_a_success_redirects_home_with_concise_confirmation(tmp_path) -> None:
    complete_catalog(tmp_path)
    checkout = CheckoutSessions(returned_plan="a")
    app = create_app(
        settings(tmp_path),
        SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=checkout))),
    )

    success = request(
        app,
        "GET",
        "/success?session_id=cs_test_plan_a",
        follow_redirects=False,
    )
    home = request(app, "GET", success.headers["location"])

    assert success.status_code == 303
    assert success.headers["location"] == "/?checkout=plan-a-complete"
    assert "Plan A Checkout complete" in home.text
    assert "Plan A test subscription created" in home.text
    assert "Report Plan B usage" in home.text
    assert 'id="usage"' in home.text
    assert 'name="subscription_id"' in home.text
