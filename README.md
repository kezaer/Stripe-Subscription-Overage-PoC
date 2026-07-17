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

Clone the repository, enter it:

```sh
git clone https://github.com/kezaer/Stripe-Subscription-Overage-PoC.git
cd Stripe-Subscription-Overage-PoC
```

Run one command:

```sh
uv run streaming-billing demo
```

If `STRIPE_SECRET_KEY` is absent, the command asks for the test key through a hidden prompt. It does
not save the key to disk. The command then:

1. installs the locked project dependencies through `uv`;
2. creates or validates the Products, Prices, Billing Meter, Coupon, and Promotion Code;
3. derives Checkout return URLs from the host and port;
4. starts the loopback-only app at [http://127.0.0.1:8000](http://127.0.0.1:8000);
5. opens the app in your browser.

Press `Ctrl+C` in the terminal to stop it. Use `--no-open` when you do not want the command to open
a browser:

```sh
uv run streaming-billing demo --no-open
```

Basic mode does not require the Stripe CLI or a webhook signing secret. It covers catalog setup,
Checkout, pricing, and Plan B Meter Event submission. Use the full mode to test event-driven
subscription access.

## Start the full webhook demo

Install the [Stripe CLI](https://docs.stripe.com/stripe-cli), then run:

```sh
uv run streaming-billing demo --with-webhooks
```

The demo passes the same test key to the Stripe CLI through the child process environment. It starts
a listener for snapshot and thin events, reads the temporary signing secret, configures the local
app, and stops the listener when the app exits. You do not need `stripe login` or a copied
`whsec_...` value.

## Demo data

Use these Stripe test cards with any future expiry date and any three-digit CVC:

| Scenario | Card number |
|---|---|
| Successful payment | `4242 4242 4242 4242` |
| Authentication required | `4000 0025 0000 3155` |
| Insufficient funds | `4000 0000 0000 9995` |

Stripe lists more cases in its [test card documentation](https://docs.stripe.com/testing#cards).

Enter `LAUNCH20` to apply 20% off the eligible base charge on the first invoice.

## Try both plans

### Plan A

1. Select Plan A.
2. Enter `LAUNCH20` in Checkout if you want to test 20% off the first invoice.
3. Complete Stripe Checkout and note the returned Customer and Subscription IDs.
4. Inspect the new Customer, Subscription, and invoice in the Stripe Dashboard.
5. In full webhook mode, return to the app and expand the verified `invoice.paid` and subscription
   events. The Checkout return page confirms the browser flow, while verified webhooks govern access.

### Plan B and usage

1. Select Plan B and enter `LAUNCH20` in Checkout if you want to test 20% off the eligible base line
   on the first invoice.
2. Complete Checkout and return to the app.
3. Choose the customer and active Plan B subscription from the Stripe-backed dropdown, then enter
   the current cycle's total GB in the usage form.
4. Submit the cycle total, then inspect the local receipt and Stripe Meter Event Summary.

`LAUNCH20` applies to the Plan A Product and Plan B base Product. Plan B metered overage appears on a
later invoice and falls outside this first-invoice offer.

The form accepts cycle-to-date total usage. It subtracts the included 100 GB and sends the excess
to Stripe. Examples:

| Total cycle usage | Meter value | Billable blocks | Plan B pre-tax total before coupon |
|---:|---:|---:|---:|
| 100 GB | 0 | 0 | $10.99 |
| 101 GB | 1 | 1 | $11.99 |
| 110 GB | 10 | 1 | $11.99 |
| 111 GB | 11 | 2 | $12.99 |

Stripe queues Meter Events before aggregation. A submitted receipt means Stripe accepted the
request. Use the `usage-status` command to check whether the current aggregate matches the submitted
value.

## Documentation

- [Implementation guide](docs/implementation-guide.md)
- [Business decisions](docs/business-decisions.md)

The integration pins Stripe API version `2026-06-24.dahlia` through `stripe-python` 15.3.1 and uses
flexible billing mode for new subscriptions.

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
