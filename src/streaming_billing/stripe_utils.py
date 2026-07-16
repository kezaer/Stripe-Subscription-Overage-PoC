from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stripe import StripeClient

from .config import STRIPE_API_VERSION, Settings


def stripe_client(settings: Settings) -> StripeClient:
    return StripeClient(
        settings.stripe_secret_key,
        stripe_version=STRIPE_API_VERSION,
        max_network_retries=2,
    )


def object_value(obj: object, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def require_test_object(obj: object, label: str) -> None:
    if object_value(obj, "livemode") is not False:
        raise RuntimeError(f"Stripe returned a non-test {label}; stopping.")


def object_id(obj: object, label: str) -> str:
    value = object_value(obj, "id")
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Stripe returned a {label} without an ID.")
    return value
