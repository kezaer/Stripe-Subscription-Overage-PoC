from __future__ import annotations

import re
from uuid import UUID


class InputError(ValueError):
    """Report invalid data before it reaches Stripe."""


def require_stripe_id(value: str, prefix: str, label: str) -> str:
    if "REPLACE" in value.upper() or not re.fullmatch(rf"{re.escape(prefix)}[A-Za-z0-9_]+", value):
        raise InputError(f"{label} must start with {prefix} and contain a Stripe object ID.")
    return value


def require_coupon_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,500}", value):
        raise InputError("coupon ID has an invalid format")
    return value


def require_checkout_session_id(value: str) -> str:
    return require_stripe_id(value, "cs_test_", "session_id")


def require_checkout_request_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise InputError("checkout_request_id must be a UUID") from exc
    if str(parsed) != value.lower():
        raise InputError("checkout_request_id must use canonical UUID form")
    return str(parsed)


def require_event_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", value):
        raise InputError(
            "event_id must use 1 to 100 letters, digits, periods, underscores, colons, or dashes"
        )
    return value


def require_period_key(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value):
        raise InputError("period must use 1 to 64 safe characters")
    return value
