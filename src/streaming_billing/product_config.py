"""Stripe catalog and commercial configuration for the proof of concept.

This is the first file to change when adapting the PoC. Amounts are expressed
in the currency's smallest unit (for USD, cents). When changing immutable
Price, Meter, or promotion terms, also version the related lookup key, event
name, or promotion code, then use a fresh ``STATE_DIR``. That keeps discovery
from mistaking the old test catalog for the new configuration.
"""

from __future__ import annotations

from decimal import Decimal

# Shared billing choices
CURRENCY = "usd"
BILLING_INTERVAL = "month"
TAX_BEHAVIOR = "exclusive"
BILLING_MODE = "flexible"
ALLOW_PROMOTION_CODES = True

# Metadata makes PoC-created objects easy to identify in Stripe.
CATALOG_METADATA = {"sample": "stripe_subscription_overage_poc"}
PRODUCT_PLAN_METADATA_KEY = "plan"
PRODUCT_COMPONENT_METADATA_KEY = "component"
PLAN_METADATA_KEY = "streaming_plan"
PLAN_A_CODE = "a"
PLAN_B_CODE = "b"
PLAN_B_BASE_COMPONENT = "base"
PLAN_B_OVERAGE_COMPONENT = "overage"

# Plan A: one flat monthly fee for unlimited usage.
PLAN_A_NAME = "Streaming Plan A"
PLAN_A_DESCRIPTION = "Unlimited monthly streaming usage"
PLAN_A_MONTHLY_CENTS = 2_499
PLAN_A_LOOKUP_KEY = "streaming_plan_a_monthly_v1"
PLAN_A_PRICE_NICKNAME = "Plan A monthly"

# Plan B base: monthly access including the configured allowance.
PLAN_B_BASE_NAME = "Streaming Plan B base"
PLAN_B_INCLUDED_GB = Decimal("100")
PLAN_B_BASE_MONTHLY_CENTS = 1_099
PLAN_B_BASE_LOOKUP_KEY = "streaming_plan_b_base_monthly_v1"
PLAN_B_BASE_PRICE_NICKNAME = "Plan B monthly base"

# Plan B overage: one fee for each started package above the allowance.
PLAN_B_OVERAGE_NAME = "Streaming Plan B overage"
PLAN_B_OVERAGE_PACKAGE_GB = 10
PLAN_B_OVERAGE_PACKAGE_CENTS = 100
PLAN_B_OVERAGE_LOOKUP_KEY = "streaming_plan_b_overage_10gb_v1"
PLAN_B_OVERAGE_PRICE_NICKNAME = "Plan B overage per started 10 GB"

# Stripe Billing Meter. ``last`` means each event is a cycle-to-date snapshot.
METER_DISPLAY_NAME = "Plan B cycle-to-date excess GB"
METER_EVENT_NAME = "streaming_cycle_excess_gb_v1"
METER_AGGREGATION = "last"
METER_CUSTOMER_PAYLOAD_KEY = "stripe_customer_id"
METER_VALUE_PAYLOAD_KEY = "value"

# Launch offer: 20% off the first invoice for either plan. Plan B's
# metered overage is intentionally excluded: with a once-duration Coupon, the
# discount normally expires on the initial base invoice before cycle-end usage
# is invoiced. Restricting the Coupon to both base Products makes that behavior
# explicit instead of implying that later overage will also be discounted.
COUPON_NAME = "20% off the first invoice"
COUPON_PERCENT_OFF = 20
COUPON_DURATION = "once"
COUPON_APPLIES_TO = ("plan_a", "plan_b_base")
PROMOTION_CODE = "LAUNCH20"


def plan_b_base_description() -> str:
    """Build the Product copy from the actual configured allowance."""

    return f"Monthly access including {PLAN_B_INCLUDED_GB:g} GB"


def plan_b_overage_description() -> str:
    """Build the Product copy from the actual configured package price."""

    amount = PLAN_B_OVERAGE_PACKAGE_CENTS / 100
    return f"${amount:g} per started {PLAN_B_OVERAGE_PACKAGE_GB} GB above {PLAN_B_INCLUDED_GB:g} GB"
