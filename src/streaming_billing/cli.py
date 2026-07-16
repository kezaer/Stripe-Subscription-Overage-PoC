from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Sequence
from dataclasses import replace
from typing import TextIO

import stripe
import uvicorn
from dotenv import load_dotenv

from .catalog import provision_catalog
from .config import ConfigurationError, Settings, local_base_url
from .pricing import excess_gb, overage_blocks, parse_usage, plan_b_total_cents
from .state import CatalogStore, catalog_as_dict
from .stripe_utils import stripe_client
from .usage import UsageLedger, reconcile_usage, report_usage, usage_update_from_subscription
from .validation import InputError

_THIN_EVENTS = (
    "v1.billing.meter.error_report_triggered",
    "v1.billing.meter.no_meter_found",
)
_SNAPSHOT_EVENTS = (
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
)
_WEBHOOK_SECRET_SEARCH = re.compile(r"whsec_[A-Za-z0-9]{16,}")
_SENSITIVE_VALUE_SEARCH = re.compile(r"(?:sk_test_|whsec_)[A-Za-z0-9]+")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stripe subscription overage proof of concept")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("setup", help="provision or resume the Stripe test catalog")

    demo = commands.add_parser(
        "demo",
        help="provision the catalog and start the one-command local PoC",
    )
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8000)
    demo.add_argument("--no-open", action="store_true", help="do not open a browser")
    demo.add_argument(
        "--with-webhooks",
        action="store_true",
        help="start and manage a Stripe CLI listener for the complete lifecycle PoC",
    )

    serve = commands.add_parser("serve", help="start the local FastAPI PoC")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    pricing = commands.add_parser("pricing", help="calculate Plan B pricing without Stripe")
    pricing.add_argument("--total-gb", required=True)

    usage = commands.add_parser("usage", help="send a cycle-to-date Plan B usage snapshot")
    usage.add_argument("--subscription", required=True)
    usage.add_argument("--revision", required=True, type=int)
    usage.add_argument("--total-gb", required=True)
    usage.add_argument("--event-id", required=True, help="stable ID for this logical update")

    status = commands.add_parser("usage-status", help="check Stripe's aggregated Meter summary")
    status.add_argument("--subscription", required=True)
    status.add_argument("--wait-seconds", type=int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "pricing":
            total_gb = parse_usage(args.total_gb)
            _print_json(
                {
                    "total_gb": str(total_gb),
                    "reported_excess_gb": excess_gb(total_gb),
                    "billable_10gb_blocks": overage_blocks(total_gb),
                    "projected_pre_tax_cents": plan_b_total_cents(total_gb),
                }
            )
            return 0

        if args.command == "serve":
            _serve(args.host, args.port, args.reload)
            return 0

        if args.command == "demo":
            _demo(args.host, args.port, args.no_open, args.with_webhooks)
            return 0

        settings = Settings.from_env()
        client = stripe_client(settings)
        if args.command == "setup":
            store = CatalogStore(settings.state_dir)
            catalog = provision_catalog(client, store)
            _print_json(
                {
                    "state_file": str(store.path),
                    "resources": catalog_as_dict(catalog),
                }
            )
            return 0

        if args.command == "usage-status":
            catalog = CatalogStore(settings.state_dir).catalog()
            _print_json(
                reconcile_usage(
                    client,
                    catalog,
                    UsageLedger(settings.state_dir),
                    args.subscription,
                    args.wait_seconds,
                )
            )
            return 0

        catalog = CatalogStore(settings.state_dir).catalog()
        update = usage_update_from_subscription(
            client,
            catalog,
            args.subscription,
            args.revision,
            args.event_id,
            parse_usage(args.total_gb),
        )
        _print_json(report_usage(client, UsageLedger(settings.state_dir), update))
        return 0
    except (ConfigurationError, InputError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except stripe.StripeError as exc:
        message = getattr(exc, "user_message", None) or "Stripe rejected the request."
        print(f"error: {message}", file=sys.stderr)
        return 3


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _serve(host: str, port: int, reload: bool) -> None:
    base_url = local_base_url(host, port)
    settings = Settings.from_env(require_webhook=False, base_url=base_url)
    if reload:
        # The import-string factory runs in a child process, so give it the same
        # origin derived from the server binding rather than a stale .env value.
        os.environ["APP_BASE_URL"] = settings.base_url
        uvicorn.run(
            "streaming_billing.web:app_factory",
            factory=True,
            host=host,
            port=port,
            reload=True,
            access_log=False,
        )
        return

    from .web import create_app

    uvicorn.run(create_app(settings), host=host, port=port, access_log=False)


def _demo(host: str, port: int, no_open: bool, with_webhooks: bool) -> None:
    base_url = local_base_url(host, port)
    settings = _demo_settings(base_url)
    client = stripe_client(settings)
    store = CatalogStore(settings.state_dir)
    catalog = provision_catalog(client, store)

    print(f"Catalog ready ({len(catalog_as_dict(catalog))} Stripe test objects).")
    listener: StripeCliListener | None = None
    browser_timer: threading.Timer | None = None
    try:
        if with_webhooks:
            listener = StripeCliListener(settings.stripe_secret_key, settings.base_url)
            webhook_secret = listener.start()
            settings = replace(
                settings,
                webhook_secret=webhook_secret,
                thin_webhook_secret=webhook_secret,
            )
            print("Stripe CLI listener ready for snapshot and thin events.")
        else:
            print("Basic mode: Checkout works now. Add --with-webhooks for lifecycle events.")

        if not no_open:
            browser_timer = threading.Timer(0.75, webbrowser.open, args=(settings.base_url,))
            browser_timer.daemon = True
            browser_timer.start()

        from .web import create_app

        print(f"PoC: {settings.base_url} (press Ctrl+C to stop)")
        uvicorn.run(
            create_app(settings, client),
            host=host,
            port=port,
            access_log=False,
        )
    finally:
        if browser_timer is not None:
            browser_timer.cancel()
        if listener is not None:
            listener.stop()


def _demo_settings(base_url: str) -> Settings:
    load_dotenv(override=False)
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key or key == "sk_test_replace_me":
        key = getpass.getpass("Stripe test secret key (sk_test_...): ").strip()
        if not key:
            raise ConfigurationError("A Stripe test secret key is required.")
        # Keep the key process-local. It is never written to disk or included in
        # a command-line argument.
        os.environ["STRIPE_SECRET_KEY"] = key
    return Settings.from_env(load_env_file=False, base_url=base_url)


class StripeCliListener:
    """Own one Stripe CLI listener and capture its ephemeral signing secret."""

    def __init__(self, stripe_secret_key: str, base_url: str) -> None:
        self.stripe_secret_key = stripe_secret_key
        self.base_url = base_url
        self.process: subprocess.Popen[str] | None = None
        self.secret: str | None = None
        self._startup_lines: list[str] = []
        self._secret_ready = threading.Event()

    def start(self) -> str:
        executable = shutil.which("stripe")
        if executable is None:
            raise ConfigurationError(
                "--with-webhooks requires the Stripe CLI. Install it from "
                "https://docs.stripe.com/stripe-cli and rerun the command."
            )

        command = [
            executable,
            "listen",
            "--skip-update",
            "--color",
            "off",
            "--events",
            ",".join(_SNAPSHOT_EVENTS),
            "--forward-to",
            f"{self.base_url}/webhooks/stripe",
            "--thin-events",
            ",".join(_THIN_EVENTS),
            "--forward-thin-to",
            f"{self.base_url}/webhooks/stripe/thin",
        ]
        child_env = os.environ.copy()
        child_env["STRIPE_API_KEY"] = self.stripe_secret_key
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=child_env,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ConfigurationError("Could not start the Stripe CLI listener.") from exc

        assert self.process.stdout is not None
        reader = threading.Thread(
            target=self._read_output,
            args=(self.process.stdout,),
            name="stripe-listener-output",
            daemon=True,
        )
        reader.start()
        self._secret_ready.wait(timeout=20)
        if self.secret is None:
            details = " ".join(self._startup_lines[-4:]).strip()
            self.stop()
            suffix = f" Stripe CLI said: {details}" if details else ""
            raise ConfigurationError(
                "Stripe CLI did not provide a webhook signing secret. Check the test key "
                f"and your network connection, then rerun --with-webhooks.{suffix}"
            )
        return self.secret

    def _read_output(self, stream: TextIO) -> None:
        try:
            for line in stream:
                match = _WEBHOOK_SECRET_SEARCH.search(line)
                if match is not None and self.secret is None:
                    self.secret = match.group(0)
                    self._secret_ready.set()
                if len(self._startup_lines) < 20:
                    self._startup_lines.append(_redact_sensitive_values(line.strip()))
        finally:
            self._secret_ready.set()

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _redact_sensitive_values(value: str) -> str:
    return _SENSITIVE_VALUE_SEARCH.sub("[REDACTED]", value)


if __name__ == "__main__":
    raise SystemExit(main())
