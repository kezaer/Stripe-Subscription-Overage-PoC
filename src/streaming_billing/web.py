from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import uuid4

import stripe
from fastapi import FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import product_config as product
from .checkout import checkout_summary, create_checkout_session
from .config import Settings
from .pricing import Plan, parse_usage
from .state import CatalogStore
from .stripe_utils import object_value, stripe_client
from .usage import (
    UsageLedger,
    meter_error_details,
    report_usage,
    usage_context_from_subscription,
)
from .validation import InputError, require_stripe_id
from .webhooks import (
    LifecycleStore,
    WebhookEventStore,
    event_summary,
    lifecycle_update,
    thin_event_summary,
    verify_event,
)

_PACKAGE_DIR = Path(__file__).parent
_STYLESHEET_VERSION = str((_PACKAGE_DIR / "static" / "styles.css").stat().st_mtime_ns)
_TEST_CARDS = (
    {"scenario": "Successful payment", "number": "4242 4242 4242 4242"},
    {"scenario": "Authentication required", "number": "4000 0025 0000 3155"},
    {"scenario": "Insufficient funds", "number": "4000 0000 0000 9995"},
)


def create_app(settings: Settings, client: Any | None = None) -> FastAPI:
    client = client or stripe_client(settings)
    catalog_store = CatalogStore(settings.state_dir)
    event_store = WebhookEventStore(settings.state_dir)
    lifecycle_store = LifecycleStore(settings.state_dir)
    usage_ledger = UsageLedger(settings.state_dir)
    templates = Jinja2Templates(directory=_PACKAGE_DIR / "templates")

    app = FastAPI(title="Stripe Subscription Overage PoC", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=_PACKAGE_DIR / "static"), name="static")

    def render_home(
        request: Request,
        *,
        checkout_cancelled: bool = False,
        checkout_confirmation: dict[str, str] | None = None,
        usage_subscription: str | None = None,
        usage_submitted: bool = False,
        usage_error: str | None = None,
        usage_form: dict[str, Any] | None = None,
        status_code: int = 200,
    ):
        selected_usage = None
        if usage_submitted and usage_subscription:
            try:
                selected_usage = usage_ledger.latest_public_for_subscription(usage_subscription)
            except InputError:
                usage_error = usage_error or "The submitted usage result is unavailable."
        if usage_form is None:
            if usage_subscription:
                try:
                    usage_subscription = require_stripe_id(
                        usage_subscription, "sub_", "subscription"
                    )
                except InputError as exc:
                    usage_error = usage_error or str(exc)
                    usage_subscription = None
            usage_form = {
                "subscription_id": usage_subscription or "",
                "total_gb": "",
            }
        configured = catalog_store.is_complete()
        lifecycle_rows = lifecycle_store.subscriptions()
        webhook_rows = _resolve_webhook_subjects(
            client, event_store.recent(), configured=configured
        )
        known_plan_b = lifecycle_store.plan_b_subscriptions()
        (
            plan_b_subscriptions,
            subscription_load_failed,
            subscription_directory,
        ) = _plan_b_subscription_choices(
            client,
            catalog_store,
            known_plan_b,
            configured=configured,
        )
        subscription_directory = _subscription_identity_directory(
            client,
            subscription_directory,
            [*lifecycle_rows, *webhook_rows],
            configured=configured,
        )
        usage_activity = [
            _display_usage(row, subscription_directory.get(row.get("subscription_id")))
            for row in usage_ledger.recent()
        ]
        webhook_activity = [
            _display_subscription_subject(row, _identity_for_row(row, subscription_directory))
            for row in webhook_rows
        ]
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "configured": configured,
                "checkout_cancelled": checkout_cancelled,
                "checkout_confirmation": checkout_confirmation,
                "plan_a_request_id": str(uuid4()),
                "plan_b_request_id": str(uuid4()),
                "webhook_enabled": settings.webhook_secret is not None,
                "thin_webhook_enabled": settings.thin_webhook_secret is not None,
                "events": webhook_activity,
                "webhook_activity": webhook_activity,
                "subscriptions": [
                    _display_lifecycle(row, _identity_for_row(row, subscription_directory))
                    for row in lifecycle_rows
                ],
                "plan_b_subscriptions": plan_b_subscriptions,
                "subscription_load_failed": subscription_load_failed,
                "lifecycle_history": [_display_lifecycle(row) for row in lifecycle_store.recent()],
                "usage_activity": usage_activity,
                "selected_usage": (
                    _display_usage(
                        selected_usage,
                        subscription_directory.get(selected_usage.get("subscription_id")),
                    )
                    if selected_usage
                    else None
                ),
                "usage_error": usage_error,
                "usage_form": usage_form,
                "asset_version": _STYLESHEET_VERSION,
                "test_cards": _TEST_CARDS,
                "product": {
                    "plan_a_monthly": f"{product.PLAN_A_MONTHLY_CENTS / 100:.2f}",
                    "plan_b_base_monthly": f"{product.PLAN_B_BASE_MONTHLY_CENTS / 100:.2f}",
                    "included_gb": f"{product.PLAN_B_INCLUDED_GB:g}",
                    "package_gb": product.PLAN_B_OVERAGE_PACKAGE_GB,
                    "package_price": f"{product.PLAN_B_OVERAGE_PACKAGE_CENTS / 100:.2f}",
                    "promotion_code": product.PROMOTION_CODE,
                    "promotion_percent": product.COUPON_PERCENT_OFF,
                },
            },
            status_code=status_code,
        )

    @app.get("/")
    async def index(
        request: Request,
        checkout: str | None = None,
        usage_subscription: str | None = None,
        usage_submitted: bool = False,
    ):
        return render_home(
            request,
            checkout_cancelled=checkout == "cancelled",
            checkout_confirmation=(
                {
                    "plan_code": product.PLAN_A_CODE,
                    "plan_name": "Plan A",
                    "message": "Plan A test subscription created.",
                }
                if checkout == "plan-a-complete"
                else None
            ),
            usage_subscription=usage_subscription,
            usage_submitted=usage_submitted,
        )

    @app.post("/usage")
    async def usage_route(
        request: Request,
        subscription_id: Annotated[str, Form(min_length=5, max_length=255)],
        total_gb: Annotated[str, Form(min_length=1, max_length=32)],
    ):
        form = {
            "subscription_id": subscription_id,
            "total_gb": total_gb,
        }
        try:
            catalog = catalog_store.catalog()
        except InputError as exc:
            return render_home(
                request,
                usage_error=str(exc),
                usage_form=form,
                status_code=422,
            )
        except RuntimeError as exc:
            return render_home(
                request,
                usage_error=str(exc),
                usage_form=form,
                status_code=409,
            )
        try:
            parsed_usage = parse_usage(total_gb)
            context = usage_context_from_subscription(client, catalog, subscription_id)
            update = usage_ledger.prepare_update(context, parsed_usage)
            report_usage(client, usage_ledger, update)
        except InputError as exc:
            return render_home(
                request,
                usage_error=str(exc),
                usage_form=form,
                status_code=422,
            )
        except (RuntimeError, stripe.StripeError):
            return render_home(
                request,
                usage_error=(
                    "Stripe did not confirm this request. Retry this form unchanged so the "
                    "server can safely reuse the same request identity."
                ),
                usage_form=form,
                status_code=502,
            )
        destination = "/?" + urlencode(
            {"usage_subscription": subscription_id, "usage_submitted": "true"}
        )
        return RedirectResponse(f"{destination}#usage", status_code=303)

    @app.get("/usage/{event_id}")
    async def usage_status(event_id: str):
        try:
            result = usage_ledger.by_event(event_id)
        except InputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Usage revision was not found.")
        return JSONResponse(_display_usage(result))

    @app.post("/checkout")
    async def checkout_route(
        plan: Annotated[str, Form()],
        checkout_request_id: Annotated[str, Form()],
    ):
        try:
            selected_plan = Plan.parse(plan)
            catalog = catalog_store.catalog()
            destination = create_checkout_session(
                client,
                catalog,
                selected_plan,
                settings,
                checkout_request_id,
            )
        except InputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(destination, status_code=303)

    @app.get("/success")
    async def success_route(
        request: Request,
        session_id: Annotated[str, Query(min_length=8, max_length=255)],
    ):
        try:
            summary = checkout_summary(client, session_id)
        except (InputError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if summary["plan_code"] == product.PLAN_A_CODE:
            return RedirectResponse("/?checkout=plan-a-complete", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="success.html",
            context={
                "summary": summary,
                "webhook_enabled": settings.webhook_secret is not None,
            },
        )

    @app.post("/webhooks/stripe")
    async def webhook_route(
        request: Request,
        stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
    ):
        if settings.webhook_secret is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Snapshot webhooks are optional and currently unavailable because "
                    "STRIPE_WEBHOOK_SECRET is not configured. Checkout and usage still work."
                ),
            )
        payload = await request.body()
        try:
            event = verify_event(payload, stripe_signature or "", settings.webhook_secret)
            summary = event_summary(event)
        except (ValueError, stripe.SignatureVerificationError, InputError) as exc:
            raise HTTPException(status_code=400, detail="Invalid Stripe webhook.") from exc
        claim_token = event_store.claim_with_token(summary)
        if claim_token is None:
            return JSONResponse({"received": True, "duplicate": True})
        try:
            update = lifecycle_update(event)
            updated = lifecycle_store.apply(update) if update is not None else False
            details = {
                "lifecycle_updated": updated,
                "subscription_id": update.subscription_id if update is not None else None,
            }
            completed = event_store.complete(summary["id"], details, claim_token=claim_token)
        except InputError as exc:
            event_store.release(summary["id"], claim_token=claim_token)
            raise HTTPException(status_code=400, detail="Invalid Stripe webhook object.") from exc
        except Exception as exc:
            event_store.release(summary["id"], claim_token=claim_token)
            raise HTTPException(
                status_code=502, detail="Could not process Stripe webhook."
            ) from exc
        return JSONResponse({"received": True, "duplicate": not completed})

    @app.post("/webhooks/stripe/thin")
    async def thin_webhook_route(
        request: Request,
        stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
    ):
        if settings.thin_webhook_secret is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Thin meter-error webhooks are optional and currently unavailable because "
                    "STRIPE_THIN_WEBHOOK_SECRET is not configured."
                ),
            )
        payload = await request.body()
        try:
            notification = client.parse_event_notification(
                payload.decode("utf-8"), stripe_signature or "", settings.thin_webhook_secret
            )
            summary = thin_event_summary(notification)
        except (ValueError, stripe.SignatureVerificationError, InputError) as exc:
            raise HTTPException(status_code=400, detail="Invalid Stripe thin webhook.") from exc
        claim_token = event_store.claim_with_token(summary)
        if claim_token is None:
            return JSONResponse({"received": True, "duplicate": True})
        try:
            event = notification.fetch_event()
            errors_by_identifier = meter_error_details(event)
            matched = usage_ledger.mark_errors(errors_by_identifier, summary["id"])
            completed = event_store.complete(
                summary["id"],
                {"matched_usage_updates": matched},
                claim_token=claim_token,
            )
        except Exception as exc:
            event_store.release(summary["id"], claim_token=claim_token)
            raise HTTPException(
                status_code=502, detail="Could not process Stripe thin event."
            ) from exc
        return JSONResponse({"received": True, "duplicate": not completed, "matched": matched})

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "catalog_configured": catalog_store.is_complete(),
            "snapshot_webhooks_configured": settings.webhook_secret is not None,
            "thin_webhooks_configured": settings.thin_webhook_secret is not None,
        }

    return app


def app_factory() -> FastAPI:
    return create_app(Settings.from_env(require_webhook=False))


def _plan_b_subscription_choices(
    client: Any,
    catalog_store: CatalogStore,
    fallback_rows: list[dict[str, Any]],
    *,
    configured: bool,
) -> tuple[list[dict[str, Any]], bool, dict[str, dict[str, Any]]]:
    """List every reportable Plan B subscription without persisting customer PII."""

    fallback = [_fallback_subscription_choice(row) for row in fallback_rows]
    fallback_directory = {choice["subscription_id"]: choice for choice in fallback}
    if not configured:
        return fallback, False, fallback_directory
    try:
        catalog = catalog_store.catalog()
        page = client.v1.subscriptions.list(
            {
                "price": catalog.plan_b_overage_price,
                "status": "all",
                "limit": 100,
                "expand": ["data.customer"],
            }
        )
        iterator = (
            page.auto_paging_iter()
            if callable(getattr(page, "auto_paging_iter", None))
            else object_value(page, "data", [])
        )
        choices = []
        directory = {}
        seen = set()
        for subscription in iterator:
            subscription_id = object_value(subscription, "id")
            if not isinstance(subscription_id, str) or subscription_id in seen:
                continue
            try:
                subscription_id = require_stripe_id(subscription_id, "sub_", "subscription")
            except InputError:
                continue
            if object_value(subscription, "livemode") is not False:
                continue
            customer = object_value(subscription, "customer", {})
            customer_id = customer if isinstance(customer, str) else object_value(customer, "id")
            status = str(object_value(subscription, "status"))
            choice = {
                "subscription_id": subscription_id,
                "customer_id": customer_id if isinstance(customer_id, str) else None,
                "customer_name": (
                    None if isinstance(customer, str) else _optional_text(customer, "name")
                ),
                "customer_email": (
                    None if isinstance(customer, str) else _optional_text(customer, "email")
                ),
                "status": status,
                "status_label": status.capitalize(),
            }
            directory[subscription_id] = choice
            if status in {"active", "trialing"}:
                choices.append(choice)
            seen.add(subscription_id)
        return choices, False, directory
    except (AttributeError, RuntimeError, stripe.StripeError):
        # Keep the page usable during a transient Stripe outage. These fallback IDs came
        # from signature-verified lifecycle events, but do not contain customer PII.
        return fallback, True, fallback_directory


def _fallback_subscription_choice(row: dict[str, Any]) -> dict[str, Any]:
    choice = _display_lifecycle(row)
    choice.update(
        {
            "customer_name": None,
            "customer_email": None,
            "status_label": choice["subscription_status_label"],
        }
    )
    return choice


def _resolve_webhook_subjects(
    client: Any,
    rows: list[dict[str, Any]],
    *,
    configured: bool,
) -> list[dict[str, Any]]:
    """Resolve customer context for historical observed-only event rows.

    New deliveries retain direct customer and subscription IDs during ingestion.
    Older rows only have their primary Stripe object ID, so supported objects are
    expanded from Stripe at render time without persisting customer PII.
    """

    if not configured:
        return [dict(row) for row in rows]
    cache: dict[str, dict[str, Any]] = {}
    result = []
    for row in rows:
        current = dict(row)
        if current.get("customer_id"):
            result.append(current)
            continue
        resource_id = current.get("resource_id")
        if not isinstance(resource_id, str):
            result.append(current)
            continue
        if resource_id not in cache:
            try:
                cache[resource_id] = _webhook_subject_for_resource(client, resource_id)
            except (AttributeError, RuntimeError, InputError, stripe.StripeError):
                cache[resource_id] = {}
        current.update(cache[resource_id])
        result.append(current)
    return result


def _webhook_subject_for_resource(client: Any, resource_id: str) -> dict[str, Any]:
    if resource_id.startswith("cus_"):
        return {"customer_id": require_stripe_id(resource_id, "cus_", "customer")}
    if resource_id.startswith("inpay_"):
        invoice_payment = client.v1.invoice_payments.retrieve(
            resource_id, {"expand": ["invoice.customer"]}
        )
        if not _test_object_matches(invoice_payment, resource_id):
            return {}
        invoice = object_value(invoice_payment, "invoice", {})
        return _subject_from_invoice(invoice)
    if resource_id.startswith("in_"):
        invoice = client.v1.invoices.retrieve(resource_id, {"expand": ["customer"]})
        return _subject_from_invoice(invoice, expected_id=resource_id)

    service_name = None
    if resource_id.startswith("pi_"):
        service_name = "payment_intents"
    elif resource_id.startswith("pm_"):
        service_name = "payment_methods"
    elif resource_id.startswith("ch_"):
        service_name = "charges"
    if service_name is None:
        return {}
    stripe_object = getattr(client.v1, service_name).retrieve(resource_id, {"expand": ["customer"]})
    if not _test_object_matches(stripe_object, resource_id):
        return {}
    return _subject_from_customer(object_value(stripe_object, "customer"))


def _subject_from_invoice(invoice: object, *, expected_id: str | None = None) -> dict[str, Any]:
    invoice_id = object_value(invoice, "id")
    if (
        not isinstance(invoice_id, str)
        or (expected_id is not None and invoice_id != expected_id)
        or object_value(invoice, "livemode") is not False
    ):
        return {}
    invoice_id = require_stripe_id(invoice_id, "in_", "invoice")
    result = {"invoice_id": invoice_id, **_subject_from_customer(object_value(invoice, "customer"))}
    subscription = object_value(invoice, "subscription")
    if subscription is None:
        parent = object_value(invoice, "parent", {})
        details = object_value(parent, "subscription_details", {})
        subscription = object_value(details, "subscription")
    subscription_id = (
        subscription if isinstance(subscription, str) else object_value(subscription, "id")
    )
    if isinstance(subscription_id, str):
        result["subscription_id"] = require_stripe_id(subscription_id, "sub_", "subscription")
    return result


def _subject_from_customer(customer: object) -> dict[str, Any]:
    customer_id = customer if isinstance(customer, str) else object_value(customer, "id")
    if not isinstance(customer_id, str):
        return {}
    result = {
        "customer_id": require_stripe_id(customer_id, "cus_", "customer"),
        "customer_name": None,
        "customer_email": None,
    }
    if not isinstance(customer, str):
        result["customer_name"] = _optional_text(customer, "name")
        result["customer_email"] = _optional_text(customer, "email")
    return result


def _test_object_matches(stripe_object: object, expected_id: str) -> bool:
    return (
        object_value(stripe_object, "id") == expected_id
        and object_value(stripe_object, "livemode") is False
    )


def _subscription_identity_directory(
    client: Any,
    directory: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    configured: bool,
) -> dict[str, dict[str, Any]]:
    """Best-effort customer identity for locally tracked subscriptions.

    Customer PII remains in Stripe: the page resolves it on demand and only the
    allowlisted name and email fields reach the template.
    """

    result = {subscription_id: dict(identity) for subscription_id, identity in directory.items()}
    if not configured:
        return result

    for row in rows:
        customer_id = row.get("customer_id")
        subscription_id = row.get("subscription_id")
        identity = {
            "subscription_id": subscription_id,
            "customer_id": customer_id,
            "customer_name": row.get("customer_name"),
            "customer_email": row.get("customer_email"),
        }
        keys = []
        if isinstance(subscription_id, str):
            keys.append(subscription_id)
        if isinstance(customer_id, str):
            keys.append(f"customer:{customer_id}")
        for key in keys:
            current = result.setdefault(key, {})
            for field, value in identity.items():
                if value and not current.get(field):
                    current[field] = value

    subscription_ids = {
        row.get("subscription_id") for row in rows if isinstance(row.get("subscription_id"), str)
    }
    for subscription_id in sorted(subscription_ids):
        current = result.get(subscription_id, {})
        if current.get("customer_name") or current.get("customer_email"):
            continue
        try:
            subscription = client.v1.subscriptions.retrieve(
                subscription_id, {"expand": ["customer"]}
            )
            if (
                object_value(subscription, "id") != subscription_id
                or object_value(subscription, "livemode") is not False
            ):
                continue
            customer = object_value(subscription, "customer", {})
            customer_id = customer if isinstance(customer, str) else object_value(customer, "id")
            result[subscription_id] = {
                **current,
                "subscription_id": subscription_id,
                "customer_id": customer_id if isinstance(customer_id, str) else None,
                "customer_name": (
                    None if isinstance(customer, str) else _optional_text(customer, "name")
                ),
                "customer_email": (
                    None if isinstance(customer, str) else _optional_text(customer, "email")
                ),
            }
        except (AttributeError, RuntimeError, stripe.StripeError):
            continue

    customer_ids = {
        row.get("customer_id") for row in rows if isinstance(row.get("customer_id"), str)
    }
    for customer_id in sorted(customer_ids):
        customer_key = f"customer:{customer_id}"
        current = result.get(customer_key, {})
        if current.get("customer_name") or current.get("customer_email"):
            continue
        try:
            customer = client.v1.customers.retrieve(customer_id)
            if not _test_object_matches(customer, customer_id):
                continue
            resolved = _subject_from_customer(customer)
            result[customer_key] = {**current, **resolved}
            for identity in result.values():
                if identity.get("customer_id") != customer_id:
                    continue
                identity["customer_name"] = resolved.get("customer_name")
                identity["customer_email"] = resolved.get("customer_email")
        except (AttributeError, RuntimeError, InputError, stripe.StripeError):
            continue
    return result


def _identity_for_row(
    row: dict[str, Any], directory: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    subscription_id = row.get("subscription_id")
    if isinstance(subscription_id, str) and subscription_id in directory:
        return directory[subscription_id]
    customer_id = row.get("customer_id")
    if isinstance(customer_id, str):
        identity = next(
            (
                identity
                for identity in directory.values()
                if identity.get("customer_id") == customer_id
            ),
            None,
        )
        if identity:
            # A customer can own several subscriptions. Never infer one for an event
            # that only carries a customer reference.
            return {
                "customer_id": customer_id,
                "customer_name": identity.get("customer_name"),
                "customer_email": identity.get("customer_email"),
            }
    return None


def _optional_text(obj: object, key: str) -> str | None:
    value = object_value(obj, key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _display_usage(
    row: dict[str, Any], subscription: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = dict(row)
    result["customer_name"] = subscription.get("customer_name") if subscription else None
    result["customer_email"] = subscription.get("customer_email") if subscription else None
    cents = int(result.get("projected_pre_tax_cents", 0))
    result["projected_pre_tax"] = f"${cents / 100:.2f}"
    status = result.get("status")
    result["status_label"] = {
        "submitted": "Accepted by Stripe API",
        "confirmed": "Matched in a Stripe period aggregate",
        "rejected": "Rejected asynchronously",
        "pending": "Pending submission",
    }.get(status, str(status or "Unknown"))
    return result


def _display_subscription_subject(
    row: dict[str, Any], subscription: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = dict(row)
    if subscription:
        for field in (
            "subscription_id",
            "customer_id",
            "customer_name",
            "customer_email",
        ):
            if subscription.get(field):
                result[field] = subscription[field]
    result.setdefault("customer_name", None)
    result.setdefault("customer_email", None)
    relations = [
        dict(relation)
        for relation in result.get("object_relations", [])
        if isinstance(relation, dict)
    ]
    seen = {relation.get("id") for relation in relations}
    for object_type, field in (
        ("Invoice", "invoice_id"),
        ("Subscription", "subscription_id"),
        ("Customer", "customer_id"),
    ):
        object_id = result.get(field)
        if isinstance(object_id, str) and object_id not in seen:
            relations.append({"object_type": object_type, "id": object_id})
            seen.add(object_id)
    result["object_relations"] = relations
    return result


def _display_lifecycle(
    row: dict[str, Any], subscription: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = _display_subscription_subject(row, subscription)
    access_state = str(result.get("access_state") or "unknown")
    result["access_label"] = {
        "active": "Active",
        "awaiting_invoice": "Waiting for payment confirmation",
        "payment_issue": "Payment issue",
        "inactive": "Inactive",
        "unknown": "Not determined",
    }.get(access_state, access_state.replace("_", " ").capitalize())
    subscription_status = result.get("subscription_status")
    result["subscription_status_label"] = (
        str(subscription_status).replace("_", " ").capitalize()
        if subscription_status
        else "Subscription status event not received"
    )
    created = result.get("created", result.get("last_event_created"))
    if isinstance(created, int):
        result["created_display"] = datetime.fromtimestamp(created, UTC).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    amount = result.get("amount_paid", result.get("latest_amount_paid"))
    currency = result.get("currency")
    if isinstance(amount, int) and isinstance(currency, str):
        result["amount_paid_display"] = f"{currency.upper()} {amount / 100:.2f}"
    return result
