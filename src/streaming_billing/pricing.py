from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from . import product_config as product
from .product_config import (
    PLAN_A_MONTHLY_CENTS,
    PLAN_B_BASE_MONTHLY_CENTS,
    PLAN_B_INCLUDED_GB,
    PLAN_B_OVERAGE_PACKAGE_CENTS,
    PLAN_B_OVERAGE_PACKAGE_GB,
)
from .validation import InputError

# Compatibility aliases keep the small pricing API stable for existing users.
CURRENCY = product.CURRENCY
METER_EVENT_NAME = product.METER_EVENT_NAME
INCLUDED_GB = PLAN_B_INCLUDED_GB
PACKAGE_GB = PLAN_B_OVERAGE_PACKAGE_GB
PLAN_A_CENTS = PLAN_A_MONTHLY_CENTS
PLAN_B_BASE_CENTS = PLAN_B_BASE_MONTHLY_CENTS
PLAN_B_PACKAGE_CENTS = PLAN_B_OVERAGE_PACKAGE_CENTS


class Plan(StrEnum):
    A = product.PLAN_A_CODE
    B = product.PLAN_B_CODE

    @classmethod
    def parse(cls, value: str) -> Plan:
        try:
            return cls(value.lower())
        except ValueError as exc:
            raise InputError("plan must be a or b") from exc


def parse_usage(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise InputError("total_gb must be a decimal number") from exc
    if not result.is_finite() or result < 0:
        raise InputError("total_gb must be finite and non-negative")
    return result


def excess_gb(total_cycle_gb: Decimal) -> int:
    if not total_cycle_gb.is_finite() or total_cycle_gb < 0:
        raise InputError("total_cycle_gb must be finite and non-negative")
    return math.ceil(max(total_cycle_gb - INCLUDED_GB, Decimal(0)))


def overage_blocks(total_cycle_gb: Decimal) -> int:
    return math.ceil(excess_gb(total_cycle_gb) / PACKAGE_GB)


def plan_b_total_cents(total_cycle_gb: Decimal) -> int:
    return PLAN_B_BASE_CENTS + overage_blocks(total_cycle_gb) * PLAN_B_PACKAGE_CENTS
