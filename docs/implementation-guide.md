# Stripe Billing implementation guide

This guide maps the implemented streaming plans to Stripe Billing and lists the Python requests used
by [the runnable proof of concept](https://github.com/kezaer/Stripe-Subscription-Overage-PoC).

## Billing model

| Plan | Stripe model | Monthly charge |
|---|---|---:|
| Plan A | One licensed monthly Price | $24.99 |
| Plan B base | One licensed monthly Price | $10.99, including 100 GB |
| Plan B overage | One metered monthly Price | $1 for each started 10 GB above 100 GB |

The sample uses USD, tax-exclusive Prices, and an anniversary billing cycle that starts when the
the subscriber completes Checkout.

The `LAUNCH20` Promotion Code gives 20% off eligible lines on the first subscription invoice. Its
Coupon applies to the Plan A Product and the Plan B base Product. Plan B usage appears on a later
invoice, so the offer does not discount metered overage. Change the Coupon duration or Product scope
if your commercial offer needs a different result.

## Required Stripe resources

Create the catalog once, save each returned ID, and reuse those IDs when you create subscriptions.
The PoC adds metadata, lookup keys, and operation-specific idempotency keys so it can recover an
existing test catalog without creating duplicate objects.

### Plan A Product and Price

```python
plan_a_product = stripe.v1.products.create(
    {
        "name": "Streaming Plan A",
        "description": "Unlimited monthly streaming usage",
    },
    {"idempotency_key": "catalog-plan-a-product-v1"},
)

plan_a_price = stripe.v1.prices.create(
    {
        "product": plan_a_product.id,
        "currency": "usd",
        "unit_amount": 2499,
        "recurring": {"interval": "month", "usage_type": "licensed"},
        "tax_behavior": "exclusive",
        "lookup_key": "streaming_plan_a_monthly_v1",
    },
    {"idempotency_key": "catalog-plan-a-price-v1"},
)
```

`unit_amount` uses the currency's smallest unit, so `2499` means $24.99.

### Plan B base Product and Price

```python
plan_b_base_product = stripe.v1.products.create(
    {
        "name": "Streaming Plan B base",
        "description": "Monthly access including 100 GB",
    },
    {"idempotency_key": "catalog-plan-b-base-product-v1"},
)

plan_b_base_price = stripe.v1.prices.create(
    {
        "product": plan_b_base_product.id,
        "currency": "usd",
        "unit_amount": 1099,
        "recurring": {"interval": "month", "usage_type": "licensed"},
        "tax_behavior": "exclusive",
        "lookup_key": "streaming_plan_b_base_monthly_v1",
    },
    {"idempotency_key": "catalog-plan-b-base-price-v1"},
)
```

### Plan B Billing Meter and overage Price

The sample sends cycle-to-date excess GB. A subscription at 111 GB has 11 excess GB. The Meter keeps the
latest value for the billing period, and the Price converts 11 GB into two started 10 GB blocks.

```python
plan_b_overage_product = stripe.v1.products.create(
    {
        "name": "Streaming Plan B overage",
        "description": "$1 per started 10 GB above 100 GB",
    },
    {"idempotency_key": "catalog-plan-b-overage-product-v1"},
)

plan_b_meter = stripe.v1.billing.meters.create(
    {
        "display_name": "Plan B cycle-to-date excess GB",
        "event_name": "streaming_cycle_excess_gb_v1",
        "default_aggregation": {"formula": "last"},
        "customer_mapping": {
            "type": "by_id",
            "event_payload_key": "stripe_customer_id",
        },
        "value_settings": {"event_payload_key": "value"},
    },
    {"idempotency_key": "catalog-plan-b-meter-v1"},
)

plan_b_overage_price = stripe.v1.prices.create(
    {
        "product": plan_b_overage_product.id,
        "currency": "usd",
        "unit_amount": 100,
        "recurring": {
            "interval": "month",
            "usage_type": "metered",
            "meter": plan_b_meter.id,
        },
        "transform_quantity": {"divide_by": 10, "round": "up"},
        "tax_behavior": "exclusive",
        "lookup_key": "streaming_plan_b_overage_10gb_v1",
    },
    {"idempotency_key": "catalog-plan-b-overage-price-v1"},
)
```

`default_aggregation.formula="last"` treats each Meter Event as a replacement for the current
cycle-to-date value. Do not send per-session deltas to this Meter.

### Coupon and Promotion Code

The Coupon defines the discount. The Promotion Code defines the string entered at Checkout.

```python
launch_coupon = stripe.v1.coupons.create(
    {
        "name": "20% off the first invoice",
        "percent_off": 20,
        "duration": "once",
        "applies_to": {
            "products": [plan_a_product.id, plan_b_base_product.id],
        },
    },
    {"idempotency_key": "catalog-launch20-coupon-v1"},
)

launch_code = stripe.v1.promotion_codes.create(
    {
        "promotion": {"type": "coupon", "coupon": launch_coupon.id},
        "code": "LAUNCH20",
    },
    {"idempotency_key": "catalog-launch20-code-v1"},
)
```

The sample leaves subscriber and redemption limits open. Add `customer`, `expires_at`,
`max_redemptions`, or `restrictions.first_time_transaction` to the Promotion Code when your campaign
needs those controls.

## Create a subscription with Checkout

Plan A uses one licensed Price. Plan B includes the licensed base Price and metered overage Price.
Omit `quantity` from the metered line item.

```python
def create_checkout_session(plan, request_id, customer_id=None):
    if plan == "a":
        line_items = [{"price": plan_a_price.id, "quantity": 1}]
    elif plan == "b":
        line_items = [
            {"price": plan_b_base_price.id, "quantity": 1},
            {"price": plan_b_overage_price.id},
        ]
    else:
        raise ValueError("plan must be 'a' or 'b'")

    params = {
        "mode": "subscription",
        "line_items": line_items,
        "allow_promotion_codes": True,
        "success_url": (
            "https://app.example.com/billing/success"
            "?session_id={CHECKOUT_SESSION_ID}"
        ),
        "cancel_url": "https://app.example.com/plans",
        "subscription_data": {
            "billing_mode": {"type": "flexible"},
            "metadata": {"streaming_plan": plan},
        },
    }
    if customer_id:
        params["customer"] = customer_id

    return stripe.v1.checkout.sessions.create(
        params,
        {"idempotency_key": f"streaming-checkout-{plan}-{request_id}"},
    )
```

Create and persist one `request_id` for each Checkout attempt. Reuse it when a network timeout leaves
the result unknown. Use a new value for a new Checkout attempt.

`allow_promotion_codes=True` displays the promotion-code field. To apply a known code on the server,
send `discounts=[{"promotion_code": launch_code.id}]` instead of collecting it at Checkout.

### Optional account-level subscription limit

Stripe Checkout can create multiple subscriptions for the same Stripe Customer object or email. If the product
expects one active subscription per subscriber, Stripe offers an optional
[Limit customers to one subscription](https://docs.stripe.com/payments/checkout/limit-subscriptions)
setting under **Checkout and Payment Links** in the Dashboard. Stripe checks the Customer object supplied
to the Checkout Session or, when none is supplied, the email entered in Checkout. For a typical
single-plan subscriber account this is a sensible default; products that allow
concurrent subscriptions can leave it disabled. This is an account-level product choice, not a
Checkout Session parameter, and the PoC does not configure it.

Read the Checkout Session on your return route to confirm the browser flow and obtain its Stripe Customer
and Subscription IDs:

```python
session = stripe.v1.checkout.sessions.retrieve(
    "cs_REPLACE_ME",
    {"expand": ["subscription"]},
)
```

Treat this response as browser confirmation. Let verified invoice and subscription webhooks govern
service access.

## Report Plan B usage

Your streaming service should keep bytes as its source measurement, convert them to the contract's
GB definition, and calculate cycle-to-date excess usage:

```text
excess_gb = max(0, cycle_to_date_gb - 100)
```

The PoC rounds fractional excess GB up before it sends an integer value. For 111 total GB, send
`value="11"`.

```python
import hashlib


identifier = "sub_123-period_2026_07-revision_4"
idempotency_key = "meter-event-" + hashlib.sha256(identifier.encode()).hexdigest()

meter_event = stripe.v1.billing.meter_events.create(
    {
        "event_name": "streaming_cycle_excess_gb_v1",
        "payload": {
            "stripe_customer_id": "cus_REPLACE_ME",
            "value": "11",
        },
        "identifier": identifier,
    },
    {"idempotency_key": idempotency_key},
)
```

Persist `identifier` and the usage revision before the API call. Reuse both values after a timeout.
Create a new identifier when the source usage changes. Stripe processes Meter Events in a queue, so
an accepted response does not confirm the final aggregate or invoice amount. Reconcile the Meter
Event Summary before invoice finalization.

```python
summary = stripe.v1.billing.meters.event_summaries.list(
    plan_b_meter.id,
    {
        "customer": "cus_REPLACE_ME",
        "start_time": 1_783_036_800,
        "end_time": 1_785_715_200,
        "limit": 1,
    },
)
```

Use the Subscription Item's billing-period timestamps for `start_time` and `end_time`. Summary values
can lag behind a Meter Event acceptance response.

The Meter maps usage through the Stripe Customer ID. The sample supports one active Plan B
subscription per Customer object. Two Plan B subscriptions under one Customer object need separate
usage dimensions or separate Customer objects.

## API calls and webhook responsibilities

Your server starts work through the Stripe API:

| Server action | Stripe resource |
|---|---|
| Create the catalog | Product, Price, Billing Meter, Coupon, Promotion Code |
| Start signup | Checkout Session |
| Read the return result | Checkout Session retrieve |
| Report Plan B usage | Meter Event |
| Reconcile usage | Meter Event Summary |

Stripe reports payment and subscription changes through webhooks:

| Webhook event | Application action |
|---|---|
| `checkout.session.completed` | Link the Checkout Session, Stripe Customer, and Subscription to your user |
| `customer.subscription.created` | Record the initial subscription status without granting paid access |
| `invoice.paid` | Grant or renew access under your access policy |
| `invoice.payment_failed` | Start your failed-payment and dunning policy |
| `customer.subscription.updated` | Sync status, cancellation settings, and period dates |
| `customer.subscription.deleted` | Remove access under your cancellation policy |
| Meter error thin events | Mark the related usage revision for review and correction |

The Checkout return page gives the subscriber browser feedback. Use verified webhook state for access
decisions because renewals, retries, and cancellations can occur without an open browser.

## Webhook processing rules

Read the raw request body and verify the `Stripe-Signature` header before parsing or storing an
event. Reject events from live mode while you test the PoC.

```python
from fastapi import Request
import stripe as stripe_module


async def verified_snapshot_event(request: Request, webhook_secret: str):
    payload = await request.body()
    signature = request.headers.get("Stripe-Signature", "")
    return stripe_module.Webhook.construct_event(
        payload,
        signature,
        webhook_secret,
    )
```

Thin Meter Event notifications use the SDK's notification parser. Fetch the related event after
signature verification so you can correlate its error details with the saved usage identifier.

```python
async def verified_meter_error(request: Request, thin_webhook_secret: str):
    payload = await request.body()
    signature = request.headers.get("Stripe-Signature", "")
    notification = stripe.parse_event_notification(
        payload,
        signature,
        thin_webhook_secret,
    )
    return notification.fetch_event()
```

Keep a unique database row for each Stripe Event ID. Return a successful response for a duplicate
after signature verification, but do not repeat the business action. Stripe can deliver events late
or out of order. Retrieve the current Stripe object when an action depends on current state, and do
not let `checkout.session.completed` replace a paid, failed, or canceled access decision.

The PoC stores a small allowlist of IDs, statuses, amounts, and timestamps. It does not store raw
webhook payloads, subscriber email addresses, descriptions, secret keys, or signatures. The managed
listener requests only the six subscription lifecycle events used by the PoC plus the two thin Meter
Event errors. The event ledger still treats unknown signed snapshot events as observed-only if a
different event destination forwards them.

## Retry and idempotency rules

- Add an idempotency key to each Product, Price, Meter, Coupon, Promotion Code, Checkout Session, and
  Meter Event create request.
- Reuse the same key when you retry the same logical operation. Use a new key when the subscriber or
  source data starts a new operation.
- Save Stripe object IDs before you create dependent resources.
- Save each usage revision and Meter Event identifier before you call Stripe.
- Deduplicate webhooks by Stripe Event ID after signature verification.
- Treat a timeout as an unknown result. Retrieve or reconcile state before you issue a new charge or
  create request with a new idempotency key.

These rules prevent retries from creating duplicate subscriptions, catalog objects, or usage
updates.

## Repository source map

| File | Responsibility |
|---|---|
| [`product_config.py`](../src/streaming_billing/product_config.py) | Commercial values and catalog names |
| [`catalog.py`](../src/streaming_billing/catalog.py) | Catalog creation, discovery, and reconciliation |
| [`checkout.py`](../src/streaming_billing/checkout.py) | Checkout Session parameters |
| [`usage.py`](../src/streaming_billing/usage.py) | Usage validation, Meter Events, and aggregate status |
| [`webhooks.py`](../src/streaming_billing/webhooks.py) | Signature verification, deduplication, event explanations, and access state |
| [`web.py`](../src/streaming_billing/web.py) | FastAPI routes and browser workflow |
| [`cli.py`](../src/streaming_billing/cli.py) | One-command setup and local server |

Review [`business-decisions.md`](business-decisions.md) before you create live objects. The PoC does
not include user authentication, Stripe Tax, usage collection from a streaming service, a hosted
billing portal, refunds, or a production worker queue.

## Stripe references

- [Build a subscriptions integration](https://docs.stripe.com/billing/subscriptions/build-subscriptions)
- [Create a Product](https://docs.stripe.com/api/products/create?lang=python)
- [Create a Price](https://docs.stripe.com/api/prices/create?lang=python)
- [Create a Billing Meter](https://docs.stripe.com/billing/subscriptions/usage-based/meters/configure)
- [Record usage](https://docs.stripe.com/billing/subscriptions/usage-based/recording-usage)
- [Create a Checkout Session](https://docs.stripe.com/api/checkout/sessions/create?lang=python)
- [Create a Coupon](https://docs.stripe.com/api/coupons/create?lang=python)
- [Create a Promotion Code](https://docs.stripe.com/api/promotion_codes/create?lang=python)
- [Receive webhooks](https://docs.stripe.com/webhooks)
