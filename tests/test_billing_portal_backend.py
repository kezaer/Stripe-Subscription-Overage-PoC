from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from types import SimpleNamespace

import httpx

from streaming_billing.config import Settings
from streaming_billing.state import CATALOG_FIELDS, CatalogStore
from streaming_billing.usage import UsageContext, UsageLedger
from streaming_billing.web import create_app
from streaming_billing.webhooks import (
    LifecycleStore,
    LifecycleUpdate,
    WebhookEventStore,
    lifecycle_update,
)


def settings(tmp_path) -> Settings:
    return Settings(
        stripe_secret_key="sk_test_" + "A" * 24,
        webhook_secret=None,
        base_url="http://127.0.0.1:8000",
        state_dir=tmp_path,
        thin_webhook_secret=None,
    )


def complete_catalog(tmp_path) -> None:
    store = CatalogStore(tmp_path)
    for name, prefix in CATALOG_FIELDS.items():
        store.save_resource(name, f"{prefix or 'coupon_'}test_{name}")


def request(app, method: str, path: str, **kwargs):
    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def test_home_clarifies_that_launch20_excludes_plan_b_overage(tmp_path) -> None:
    app = create_app(settings(tmp_path), SimpleNamespace())

    home = request(app, "GET", "/")

    assert "LAUNCH20" in home.text
    assert "20% off the first monthly plan charge for either plan" in home.text
    assert "Plan B overage is excluded" in home.text
    assert "Use any future expiry date and any three-digit CVC" in home.text
    assert "4242 4242 4242 4242" in home.text
    assert "4000 0025 0000 3155" in home.text
    assert "4000 0000 0000 9995" in home.text


def test_home_contains_step_one_heading_with_the_step_content(tmp_path) -> None:
    app = create_app(settings(tmp_path), SimpleNamespace())

    home = request(app, "GET", "/")

    step_one = home.text.split('<section class="panel plan-selection"', 1)[1].split(
        '<section class="workbench"', 1
    )[0]
    assert '<p class="eyebrow">Step 1</p>' in step_one
    assert '<h2 id="plans-heading">Choose a billing model</h2>' in step_one
    assert "Choose a plan, then complete Stripe Checkout" in step_one
    assert "Test Plan A" in step_one
    assert "Test Plan B" in step_one


def test_home_links_to_subscription_limit_guidance(tmp_path) -> None:
    app = create_app(settings(tmp_path), SimpleNamespace())

    home = request(app, "GET", "/")

    assert "multiple subscriptions for the same email" in home.text
    assert "This PoC leaves that" in home.text
    assert "behavior unchanged" in home.text
    assert "Limit customers to one subscription" in home.text
    assert "guide describes the considerations and options" in home.text
    assert 'href="https://docs.stripe.com/payments/checkout/limit-subscriptions"' in home.text


def checkout_event(
    event_id: str,
    subscription_id: str,
    plan_code: str,
    created: int,
    *,
    extra_metadata: dict[str, str] | None = None,
) -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "created": created,
        "livemode": False,
        "data": {
            "object": {
                "id": f"cs_test_{event_id}",
                "customer": f"cus_test_{event_id}",
                "subscription": subscription_id,
                "metadata": {
                    "streaming_plan": plan_code,
                    **(extra_metadata or {}),
                },
            }
        },
    }


def test_verified_plan_metadata_builds_multiple_plan_b_choices_without_pii(tmp_path) -> None:
    store = LifecycleStore(tmp_path)
    events = [
        checkout_event("evt_test_b_one", "sub_test_b_one", "b", 10),
        checkout_event(
            "evt_test_b_two",
            "sub_test_b_two",
            "b",
            11,
            extra_metadata={"internal_note": "must-not-be-saved"},
        ),
        checkout_event("evt_test_a_one", "sub_test_a_one", "a", 12),
    ]
    for event in events:
        update = lifecycle_update(event)
        assert update is not None
        store.apply(update)

    choices = store.plan_b_subscriptions()
    assert [choice["subscription_id"] for choice in choices] == [
        "sub_test_b_two",
        "sub_test_b_one",
    ]
    assert {choice["plan_code"] for choice in choices} == {"b"}
    assert "must-not-be-saved" not in str(store.recent())

    app = create_app(settings(tmp_path), SimpleNamespace())
    home = request(app, "GET", "/")
    usage_section = home.text.split('id="usage"', 1)[1].split("</section>", 1)[0]
    assert "sub_test_b_one" in usage_section
    assert "sub_test_b_two" in usage_section
    assert "sub_test_a_one" not in usage_section


def test_home_lists_reportable_plan_b_subscriptions_with_customer_identity(tmp_path) -> None:
    complete_catalog(tmp_path)
    catalog = CatalogStore(tmp_path).catalog()

    class Subscriptions:
        def __init__(self) -> None:
            self.params = None

        def list(self, params: dict):
            self.params = params
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id="sub_test_active_customer",
                        livemode=False,
                        status="active",
                        customer=SimpleNamespace(
                            id="cus_test_active_customer",
                            name="Ada Lovelace",
                            email="ada@example.com",
                        ),
                    ),
                    SimpleNamespace(
                        id="sub_test_trial_customer",
                        livemode=False,
                        status="trialing",
                        customer=SimpleNamespace(
                            id="cus_test_trial_customer",
                            name="Grace Hopper",
                            email="grace@example.com",
                        ),
                    ),
                    SimpleNamespace(
                        id="sub_test_past_due_customer",
                        livemode=False,
                        status="past_due",
                        customer=SimpleNamespace(
                            id="cus_test_past_due_customer",
                            name="Past Due",
                            email="past-due@example.com",
                        ),
                    ),
                ]
            )

    subscriptions = Subscriptions()
    ledger = UsageLedger(tmp_path, clock=lambda: 150)
    usage = ledger.prepare_update(
        UsageContext(
            subscription_id="sub_test_active_customer",
            subscription_item_id="si_test_active_customer",
            customer_id="cus_test_active_customer",
            period_start=100,
            period_end=200,
        ),
        Decimal("121"),
    )
    state, token = ledger.claim_submission(usage)
    assert state == "claimed"
    ledger.mark_submitted(usage, token or "", submitted_at=151)
    app = create_app(
        settings(tmp_path),
        SimpleNamespace(v1=SimpleNamespace(subscriptions=subscriptions)),
    )

    home = request(app, "GET", "/")

    assert home.status_code == 200
    assert '<select id="usage-subscription" name="subscription_id"' in home.text
    assert "Ada Lovelace · ada@example.com · sub_test_active_customer" in home.text
    assert "Grace Hopper · grace@example.com · sub_test_trial_customer" in home.text
    assert "Past Due" not in home.text
    assert 'id="selected-subscription"' in home.text
    assert 'href="http://test/static/styles.css?v=' in home.text
    recent_submissions = home.text.split("Recent submissions", 1)[1].split('id="webhooks"', 1)[0]
    assert "Ada Lovelace · ada@example.com ·" in recent_submissions
    assert "sub_test_active_customer" in recent_submissions
    assert "Request details" not in recent_submissions
    assert "Revision 1 · Meter Event" in recent_submissions
    assert usage.event_id in recent_submissions
    assert subscriptions.params == {
        "price": catalog.plan_b_overage_price,
        "status": "all",
        "limit": 100,
        "expand": ["data.customer"],
    }


def test_webhook_and_current_subscription_summaries_show_customer_identity(tmp_path) -> None:
    complete_catalog(tmp_path)
    subscription_id = "sub_test_customer_context"
    customer_id = "cus_test_customer_context"

    lifecycle_store = LifecycleStore(tmp_path)
    lifecycle_store.apply(
        LifecycleUpdate(
            event_id="evt_test_customer_context",
            event_type="checkout.session.completed",
            created=100,
            resource_id="cs_test_customer_context",
            subscription_id=subscription_id,
            customer_id=customer_id,
            plan_code="a",
            access_state="awaiting_invoice",
            checkout_session_id="cs_test_customer_context",
        )
    )
    WebhookEventStore(tmp_path).record(
        {
            "id": "evt_test_customer_context",
            "type": "checkout.session.completed",
            "created": 100,
            "resource_id": "cs_test_customer_context",
            "handled": True,
            "subscription_id": subscription_id,
            "customer_id": customer_id,
            "plan_code": "a",
            "access_state": "awaiting_invoice",
        }
    )

    class Subscriptions:
        def __init__(self) -> None:
            self.retrieve_calls = []

        def list(self, params: dict):
            return SimpleNamespace(data=[])

        def retrieve(self, requested_id: str, params: dict):
            self.retrieve_calls.append((requested_id, params))
            return SimpleNamespace(
                id=requested_id,
                livemode=False,
                status="active",
                customer=SimpleNamespace(
                    id=customer_id,
                    name="Lin Chen",
                    email="lin@example.com",
                ),
            )

    subscriptions = Subscriptions()
    app = create_app(
        settings(tmp_path),
        SimpleNamespace(v1=SimpleNamespace(subscriptions=subscriptions)),
    )

    home = request(app, "GET", "/")

    webhook_section = home.text.split('id="webhooks"', 1)[1].split(
        'id="current-subscriptions-heading"', 1
    )[0]
    webhook_summary = webhook_section.split("<summary>", 1)[1].split("</summary>", 1)[0]
    assert "Lin Chen" in webhook_summary
    assert "lin@example.com" in webhook_summary
    assert subscription_id in webhook_summary
    assert '<details class="technical-details" open>' in webhook_section
    technical_details = webhook_section.split('<details class="technical-details" open>', 1)[
        1
    ].split("</details>", 1)[0]
    assert "Subscriber name" in technical_details
    assert "Lin Chen" in technical_details
    assert "Subscriber email" in technical_details
    assert "lin@example.com" in technical_details
    assert subscription_id in technical_details

    current_subscription = home.text.split('id="current-subscriptions-heading"', 1)[1]
    collapsed_subscription = current_subscription.split(
        '<details class="subscription-details">', 1
    )[0]
    assert "Lin Chen" in collapsed_subscription
    assert "lin@example.com" in collapsed_subscription
    assert subscription_id in collapsed_subscription
    assert subscriptions.retrieve_calls == [(subscription_id, {"expand": ["customer"]})]


def test_observed_webhooks_resolve_subscription_or_customer_context(tmp_path) -> None:
    complete_catalog(tmp_path)
    event_store = WebhookEventStore(tmp_path)
    event_store.record(
        {
            "id": "evt_test_invoice_payment_context",
            "type": "invoice_payment.paid",
            "created": 100,
            "resource_id": "inpay_test_subscription_context",
            "handled": False,
        }
    )
    event_store.record(
        {
            "id": "evt_test_customer_only_context",
            "type": "payment_intent.created",
            "created": 101,
            "resource_id": "pi_test_customer_only_context",
            "handled": False,
        }
    )

    class Subscriptions:
        def list(self, params: dict):
            return SimpleNamespace(data=[])

    class InvoicePayments:
        def __init__(self) -> None:
            self.calls = []

        def retrieve(self, resource_id: str, params: dict):
            self.calls.append((resource_id, params))
            return SimpleNamespace(
                id=resource_id,
                livemode=False,
                invoice=SimpleNamespace(
                    id="in_test_subscription_context",
                    livemode=False,
                    customer=SimpleNamespace(
                        id="cus_test_subscription_context",
                        name="Subscription Customer",
                        email="subscription@example.com",
                    ),
                    parent=SimpleNamespace(
                        subscription_details=SimpleNamespace(
                            subscription="sub_test_subscription_context"
                        )
                    ),
                ),
            )

    class PaymentIntents:
        def __init__(self) -> None:
            self.calls = []

        def retrieve(self, resource_id: str, params: dict):
            self.calls.append((resource_id, params))
            return SimpleNamespace(
                id=resource_id,
                livemode=False,
                customer=SimpleNamespace(
                    id="cus_test_subscription_context",
                    name="Subscription Customer",
                    email="subscription@example.com",
                ),
            )

    invoice_payments = InvoicePayments()
    payment_intents = PaymentIntents()
    app = create_app(
        settings(tmp_path),
        SimpleNamespace(
            v1=SimpleNamespace(
                subscriptions=Subscriptions(),
                invoice_payments=invoice_payments,
                payment_intents=payment_intents,
            )
        ),
    )

    home = request(app, "GET", "/")

    summaries = re.findall(r"<summary>(.*?)</summary>", home.text, flags=re.DOTALL)
    invoice_summary = next(summary for summary in summaries if "invoice_payment.paid" in summary)
    assert "Subscription Customer" in invoice_summary
    assert "subscription@example.com" in invoice_summary
    assert "cus_test_subscription_context" in invoice_summary
    assert "sub_test_subscription_context" in invoice_summary

    customer_only_summary = next(
        summary for summary in summaries if "payment_intent.created" in summary
    )
    assert "Subscription Customer" in customer_only_summary
    assert "subscription@example.com" in customer_only_summary
    assert "cus_test_subscription_context" in customer_only_summary
    assert "sub_" not in customer_only_summary

    technical_sections = home.text.split('<details class="technical-details" open>')[1:]
    invoice_technical = next(
        section for section in technical_sections if "invoice_payment.paid" in section
    )
    assert "Subscriber name" in invoice_technical
    assert "Subscription Customer" in invoice_technical
    assert "Customer ID" in invoice_technical
    assert "cus_test_subscription_context" in invoice_technical
    assert "Subscription" in invoice_technical
    assert "sub_test_subscription_context" in invoice_technical

    customer_only_technical = next(
        section for section in technical_sections if "payment_intent.created" in section
    )
    assert "Subscriber name" in customer_only_technical
    assert "Subscription Customer" in customer_only_technical
    assert "Customer ID" in customer_only_technical
    assert "cus_test_subscription_context" in customer_only_technical
    assert "<dt>Subscription</dt>" not in customer_only_technical

    assert invoice_payments.calls == [
        (
            "inpay_test_subscription_context",
            {"expand": ["invoice.customer"]},
        )
    ]
    assert payment_intents.calls == [("pi_test_customer_only_context", {"expand": ["customer"]})]


def test_invoice_parent_metadata_can_identify_plan_b() -> None:
    event = {
        "id": "evt_test_invoice_plan",
        "type": "invoice.paid",
        "created": 20,
        "livemode": False,
        "data": {
            "object": {
                "id": "in_test_invoice_plan",
                "customer": "cus_test_invoice_plan",
                "parent": {
                    "subscription_details": {
                        "subscription": "sub_test_invoice_plan",
                        "metadata": {"streaming_plan": "b"},
                    }
                },
                "status": "paid",
                "amount_due": 1099,
                "amount_paid": 1099,
                "currency": "usd",
            }
        },
    }

    update = lifecycle_update(event)

    assert update is not None
    assert update.plan_code == "b"
    assert update.subscription_id == "sub_test_invoice_plan"


def test_minimal_usage_form_reuses_server_identity_and_switches_subscription(tmp_path) -> None:
    complete_catalog(tmp_path)
    catalog = CatalogStore(tmp_path).catalog()

    class MeterEvents:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        def create(self, params: dict, options: dict):
            self.calls.append((params, options))
            if len(self.calls) == 1:
                raise RuntimeError("simulated lost Stripe response")
            return SimpleNamespace(identifier=params["identifier"], livemode=False)

    def subscription(subscription_id: str):
        suffix = subscription_id.removeprefix("sub_test_")
        return SimpleNamespace(
            id=subscription_id,
            livemode=False,
            status="active",
            customer=f"cus_test_{suffix}",
            items=SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id=f"si_test_{suffix}",
                        current_period_start=100,
                        current_period_end=200,
                        price=SimpleNamespace(
                            id=catalog.plan_b_overage_price,
                            recurring=SimpleNamespace(
                                usage_type="metered", meter=catalog.plan_b_meter
                            ),
                        ),
                    )
                ]
            ),
        )

    meters = MeterEvents()
    client = SimpleNamespace(
        v1=SimpleNamespace(
            subscriptions=SimpleNamespace(
                retrieve=lambda subscription_id, _: subscription(subscription_id)
            ),
            billing=SimpleNamespace(meter_events=meters),
        )
    )
    app = create_app(settings(tmp_path), client)
    first_form = {"subscription_id": "sub_test_one", "total_gb": "111"}

    failed = request(app, "POST", "/usage", data=first_form)
    retried = request(app, "POST", "/usage", data=first_form, follow_redirects=False)
    deduplicated = request(app, "POST", "/usage", data=first_form, follow_redirects=False)
    switched = request(
        app,
        "POST",
        "/usage",
        data={"subscription_id": "sub_test_two", "total_gb": "111"},
        follow_redirects=False,
    )

    assert failed.status_code == 502
    assert "server can safely reuse the same request identity" in failed.text
    assert retried.status_code == deduplicated.status_code == switched.status_code == 303
    assert len(meters.calls) == 3
    assert meters.calls[0][0]["identifier"] == meters.calls[1][0]["identifier"]
    assert meters.calls[0][1] == meters.calls[1][1]
    assert meters.calls[2][0]["identifier"] != meters.calls[1][0]["identifier"]
    assert meters.calls[2][0]["payload"]["stripe_customer_id"] == "cus_test_two"

    home = request(app, "GET", "/")
    assert 'name="subscription_id"' in home.text
    assert 'name="total_gb"' in home.text
    assert 'name="revision"' not in home.text
    assert 'name="event_id"' not in home.text
