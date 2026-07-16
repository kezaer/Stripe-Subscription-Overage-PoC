# Business decisions

The PoC uses the choices below to create Stripe Prices and a Billing Meter. Review them before you
adapt the implementation.

## Currency and tax

The PoC uses USD and tax-exclusive Prices. It does not enable Stripe Tax, assign Product tax codes,
or collect a billing address for tax purposes.

Confirm:

- the settlement and display currencies;
- whether advertised prices include or exclude tax;
- whether the business will use Stripe Tax;
- which Product tax codes and location evidence apply.

## Billing cycle

Checkout starts an anniversary cycle on the subscription date. A shared calendar billing date needs
a billing-cycle anchor and a decision about the first partial period.

Confirm how the business should charge during upgrades, downgrades, cancellation, and reactivation.
Licensed base fees can create prorations. Metered usage follows the usage-based plan-change rules.

## Concurrent subscriptions

Stripe Checkout can create multiple subscriptions for the same Stripe Customer object or email. If a subscriber
should hold only one active plan at a time, Stripe offers an optional
[Limit customers to one subscription](https://docs.stripe.com/payments/checkout/limit-subscriptions)
setting under **Checkout and Payment Links** in the Dashboard. Stripe can identify the subscriber
from a Customer object supplied to Checkout or from the email entered there. Decide whether Plan A and
Plan B are mutually exclusive: enabling the setting is a sensible default when they are, while
products that intentionally support concurrent subscriptions can leave it disabled. This PoC does
not enforce or configure that account-level choice.

## Overage unit and rounding

The PoC charges each started 10 GB block above 100 GB:

- 100 GB costs $10.99.
- 100.01 GB costs $11.99.
- 110 GB costs $11.99.
- 110.01 GB costs $12.99.

A completed-block rule or a prorated $0.10-per-GB rule needs different application and Price logic.

The PoC treats GB as a decimal unit supplied by the caller. The streaming service should keep
bytes as its source value and choose one contract definition:

- decimal GB: 1,000,000,000 bytes;
- binary GiB: 1,073,741,824 bytes.

Use the same definition in the product page, contract, event conversion, invoice explanation, and
support tools.

## Meter input

The Billing Meter uses `last` aggregation. Each Meter Event contains the subscription's cycle-to-date
excess GB. It does not contain a session delta or the full usage including the 100 GB allowance.

Meter Events accept whole numbers for this API path. The PoC rounds fractional excess GB up before
submission, then the Price divides the excess by 10 and rounds up to a package count.

Stripe maps these Meter Events by Stripe Customer ID. The PoC supports one active Plan B subscription
per Customer object. Two Plan B subscriptions need separate Customer objects or an attribution model
that prevents both subscription items from using the same aggregate.

## Usage timing and corrections

Stripe queues Meter Events for aggregation. Define:

- how often the service sends cycle-to-date revisions;
- the cut-off before invoice finalization;
- who reviews Meter Event errors and pending aggregates;
- how support corrects usage before and after invoice finalization.

The service should persist a durable identifier for each usage revision and reuse it during retries.
An accepted API response does not prove that the invoice includes the event.

## Coupon scope and lifetime

`LAUNCH20` gives 20% off eligible lines on the first subscription invoice for either plan.
The Coupon applies to the Plan A Product and Plan B base Product. Plan B metered overage appears on a
later invoice, so the offer does not discount usage charges.

Confirm:

- percentage or fixed-amount discount;
- Plan A, Plan B base, Plan B overage, or a combination;
- one invoice, a fixed number of months, or the subscription lifetime;
- first-purchase, subscriber, expiry, and redemption-limit restrictions.

A Coupon defines the discount economics, Product scope, and duration. A Promotion Code defines the
Checkout string and redemption controls.

## Access policy

The PoC records payment and subscription events; an adopting service needs an access policy.
Define the action for:

- `invoice.paid`;
- `invoice.payment_failed` and each dunning stage;
- scheduled and immediate cancellation;
- refunds, disputes, pauses, and unpaid invoices.

Use verified webhook state for access changes. The Checkout success redirect only provides browser
feedback.

The managed listener requests the six lifecycle events used by the PoC and the two thin Meter Event
errors. The ledger verifies and deduplicates each delivery. A secondary summary keeps one row per
Subscription ID, so repeated test Checkouts remain separate. Checkout completion can establish a
pending state, but it cannot replace a known paid, failed, or canceled outcome.

## Catalog lifecycle

Stripe lets you change a Meter's display name after creation. Its event and aggregation settings
stay fixed. A Price keeps its currency, amount, recurrence, and tax behavior.

Create versioned Products, Prices, Meters, Coupons, and Promotion Codes when those terms change.
Store test and live IDs in separate deployment configuration. Plan a subscription migration before
you deactivate an old Price or Meter.
