from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from . import product_config as product
from .config import Settings
from .pricing import Plan
from .state import Catalog
from .stripe_utils import object_id, object_value, require_test_object
from .validation import (
    InputError,
    require_checkout_request_id,
    require_checkout_session_id,
    require_stripe_id,
)


def checkout_params(catalog: Catalog, plan: Plan, settings: Settings) -> dict[str, Any]:
    if plan is Plan.A:
        line_items = [{"price": catalog.plan_a_price, "quantity": 1}]
    else:
        line_items = [
            {"price": catalog.plan_b_base_price, "quantity": 1},
            {"price": catalog.plan_b_overage_price},
        ]
    return {
        "mode": "subscription",
        "line_items": line_items,
        "allow_promotion_codes": product.ALLOW_PROMOTION_CODES,
        "success_url": settings.success_url,
        "cancel_url": settings.cancel_url,
        "metadata": {product.PLAN_METADATA_KEY: plan.value},
        "subscription_data": {
            "billing_mode": {"type": product.BILLING_MODE},
            "metadata": {product.PLAN_METADATA_KEY: plan.value},
        },
    }


def create_checkout_session(
    client: Any,
    catalog: Catalog,
    plan: Plan,
    settings: Settings,
    checkout_request_id: str,
) -> str:
    request_id = require_checkout_request_id(checkout_request_id)
    session = client.v1.checkout.sessions.create(
        checkout_params(catalog, plan, settings),
        {"idempotency_key": f"streaming-checkout-{plan.value}-{request_id}"},
    )
    require_test_object(session, "Checkout Session")
    session_url = object_value(session, "url")
    if not isinstance(session_url, str) or not _safe_checkout_url(session_url):
        raise RuntimeError("Stripe returned an invalid Checkout URL.")
    return session_url


def checkout_summary(client: Any, session_id: str) -> dict[str, str | None]:
    session_id = require_checkout_session_id(session_id)
    session = client.v1.checkout.sessions.retrieve(
        session_id,
        {"expand": ["subscription"]},
    )
    require_test_object(session, "Checkout Session")
    if object_id(session, "Checkout Session") != session_id:
        raise RuntimeError("Stripe returned a different Checkout Session.")

    customer_id = _nested_id(object_value(session, "customer"), "cus_", "customer")
    subscription = object_value(session, "subscription")
    subscription_id = _nested_id(subscription, "sub_", "subscription")
    session_plan = _metadata_plan(object_value(session, "metadata", {}))
    subscription_plan = _metadata_plan(object_value(subscription, "metadata", {}))
    if session_plan and subscription_plan and session_plan is not subscription_plan:
        raise RuntimeError("Stripe returned conflicting plan metadata.")
    plan = session_plan or subscription_plan
    return {
        "session_id": session_id,
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "status": _safe_status(object_value(session, "status")),
        "payment_status": _safe_status(object_value(session, "payment_status")),
        "plan_code": plan.value if plan is not None else None,
        "plan_name": f"Plan {plan.value.upper()}" if plan is not None else None,
    }


def _metadata_plan(metadata: object) -> Plan | None:
    value = object_value(metadata, product.PLAN_METADATA_KEY)
    if value is None:
        return None
    try:
        return Plan.parse(value)
    except InputError as exc:
        raise RuntimeError("Stripe returned invalid plan metadata.") from exc


def _nested_id(value: object, prefix: str, label: str) -> str | None:
    if value is None:
        return None
    candidate = value if isinstance(value, str) else object_value(value, "id")
    if not isinstance(candidate, str):
        raise RuntimeError(f"Stripe returned an invalid {label} reference.")
    return require_stripe_id(candidate, prefix, label)


def _safe_status(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 50:
        raise RuntimeError("Stripe returned an invalid status.")
    return value


def _safe_checkout_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname == "checkout.stripe.com"
