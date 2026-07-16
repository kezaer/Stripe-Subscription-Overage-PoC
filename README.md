# Stripe Subscription Overage PoC

A test-mode proof of concept for Stripe subscriptions with a monthly base fee and metered overage.
It demonstrates two streaming plans:

| Plan | Billing model | Price |
|---|---|---:|
| A | Flat monthly subscription | $24.99 per month |
| B | Base subscription plus metered overage | $10.99 per month including 100 GB, then $1 per started 10 GB |

The PoC provisions Stripe Products, Prices, a Billing Meter, a Coupon, and a Promotion Code. It then
opens Stripe-hosted Checkout, submits cycle-to-date usage, reconciles Meter Event summaries, and
processes signed subscription and invoice webhooks.

## Run it

Requirements:

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- a Stripe Sandbox or test-mode `sk_test_...` secret key

```sh
git clone https://github.com/kezaer/Stripe-Subscription-Overage-PoC.git
cd Stripe-Subscription-Overage-PoC
uv run streaming-billing demo
```

The command prompts for the test key without saving it, provisions the catalog, starts a
loopback-only FastAPI app, and opens [http://127.0.0.1:8000](http://127.0.0.1:8000).

Install the [Stripe CLI](https://docs.stripe.com/stripe-cli) to include signed snapshot and thin
webhook events:

```sh
uv run streaming-billing demo --with-webhooks
```

Use Stripe test card `4242 4242 4242 4242`, any future expiry date, and any three-digit CVC.
Enter `LAUNCH20` to apply 20% off the eligible base charge on the first invoice.

## Demonstrated flow

1. Choose Plan A or Plan B and complete Stripe Checkout.
2. For Plan B, submit the current billing cycle's total GB.
3. Inspect the Meter Event receipt, aggregate status, and verified webhook activity.

The PoC reports Plan B's cycle-to-date excess usage with a Billing Meter that uses `last`
aggregation. A metered Price divides excess GB by 10 and rounds up:

| Total usage | Meter value | Overage blocks | Plan B total before tax |
|---:|---:|---:|---:|
| 100 GB | 0 | 0 | $10.99 |
| 101 GB | 1 | 1 | $11.99 |
| 110 GB | 10 | 1 | $11.99 |
| 111 GB | 11 | 2 | $12.99 |

The integration pins Stripe API version `2026-06-24.dahlia` through `stripe-python` 15.3.1 and uses
flexible billing mode for new subscriptions.

## Documentation

- [Implementation guide](docs/implementation-guide.md)
- [Business decisions](docs/business-decisions.md)

## Useful commands

```sh
# Provision or reconcile the Stripe test catalog
uv run streaming-billing setup

# Start the app without the managed Stripe CLI listener
uv run streaming-billing serve

# Submit a usage revision
uv run streaming-billing usage \
  --subscription sub_REPLACE_ME \
  --revision 1 \
  --total-gb 111 \
  --event-id subscription-cycle-revision-1

# Check the Stripe Meter Event aggregate
uv run streaming-billing usage-status \
  --subscription sub_REPLACE_ME \
  --wait-seconds 10
```

## Scope

The PoC rejects live secret keys, binds to a loopback address, and stores catalog and event state in
the ignored `.local/` directory. It does not provide authentication, usage collection from a media
service, tax configuration, or durable multi-instance storage.

## Verify

```sh
uv sync --locked --all-extras
uv run ruff format --check .
uv run ruff check .
uv run pytest -q
uv build
```

## License

MIT
