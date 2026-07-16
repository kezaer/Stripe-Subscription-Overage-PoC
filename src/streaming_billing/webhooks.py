from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import stripe

from . import product_config as product
from .stripe_utils import object_value
from .validation import InputError, require_stripe_id

HANDLED_EVENT_TYPES = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.deleted",
    "customer.subscription.updated",
    "invoice.paid",
    "invoice.payment_failed",
}
_LIFECYCLE_EVENT_PRECEDENCE = {
    "checkout.session.completed": 10,
    "customer.subscription.created": 15,
    "customer.subscription.updated": 20,
    "invoice.payment_failed": 30,
    "invoice.paid": 40,
    "customer.subscription.deleted": 50,
}
THIN_EVENT_TYPES = {
    "v1.billing.meter.error_report_triggered",
    "v1.billing.meter.no_meter_found",
}

_SAFE_STATUS = re.compile(r"[a-z][a-z0-9_]{0,49}")
_SAFE_CURRENCY = re.compile(r"[a-z]{3}")

_TERMINAL_SUBSCRIPTION_STATUSES = {"canceled", "incomplete_expired", "paused"}

_EVENT_COPY = {
    "checkout.session.completed": {
        "title": "Checkout completed",
        "explanation": (
            "Stripe confirmed that the subscriber completed Checkout and created a "
            "subscription. A separate invoice event confirms payment."
        ),
        "application_action": (
            "The PoC linked the Checkout Session to the subscription. It waits for an "
            "invoice or subscription event before granting access."
        ),
        "entitlement_impact": (
            "No access change on its own. A completed Checkout never overrides a later "
            "payment or cancellation decision."
        ),
        "next_step": "Wait for the initial invoice and subscription lifecycle events.",
    },
    "invoice.paid": {
        "title": "Invoice paid",
        "explanation": "Stripe collected payment for this invoice.",
        "application_action": (
            "The PoC records the paid invoice and marks the linked subscription active "
            "unless that subscription has already been canceled."
        ),
        "entitlement_impact": "Access is granted or kept active for a non-canceled subscription.",
        "next_step": "The application can provide the subscribed service.",
    },
    "invoice.payment_failed": {
        "title": "Invoice payment failed",
        "explanation": "Stripe could not collect payment for this invoice.",
        "application_action": (
            "The PoC records the failed invoice and flags the linked subscription as "
            "having a payment issue."
        ),
        "entitlement_impact": "Access is flagged for the application's payment-failure policy.",
        "next_step": "Prompt the subscriber to update the payment method or await Stripe retries.",
    },
    "customer.subscription.created": {
        "title": "Subscription created",
        "explanation": "Stripe created the subscription and reported its initial billing status.",
        "application_action": (
            "The PoC records the allowlisted subscription status and period fields. It still "
            "waits for an invoice event before granting paid access."
        ),
        "entitlement_impact": "No access change on its own. Payment still governs paid access.",
        "next_step": "Wait for the initial invoice outcome before providing paid service.",
    },
    "customer.subscription.updated": {
        "title": "Subscription updated",
        "explanation": "Stripe reports that the subscription's billing status or terms changed.",
        "application_action": (
            "The PoC stores the allowlisted status and period fields and recalculates the "
            "subscription's access state."
        ),
        "entitlement_impact": "Access follows the new Stripe subscription status.",
        "next_step": "Review the new status and apply the corresponding service-access policy.",
    },
    "customer.subscription.deleted": {
        "title": "Subscription canceled",
        "explanation": "Stripe reports that this subscription is canceled.",
        "application_action": (
            "The PoC marks the subscription canceled and treats that decision as terminal "
            "for this subscription ID."
        ),
        "entitlement_impact": "Access is removed and later invoice events cannot restore it.",
        "next_step": "Stop service access for this subscription.",
    },
    "v1.billing.meter.error_report_triggered": {
        "title": "Meter event error reported",
        "explanation": "Stripe could not process one or more submitted meter events.",
        "application_action": (
            "The PoC matches Stripe's error identifiers to local usage submissions and "
            "marks matching submissions as rejected."
        ),
        "entitlement_impact": "No subscription access change.",
        "next_step": "Correct the rejected usage data and retry it with a new revision.",
    },
    "v1.billing.meter.no_meter_found": {
        "title": "Meter not found",
        "explanation": "Stripe could not find the meter referenced by submitted usage.",
        "application_action": (
            "The PoC matches the error to local usage submissions and marks matching "
            "submissions as rejected."
        ),
        "entitlement_impact": "No subscription access change.",
        "next_step": "Check the configured meter event name before retrying usage.",
    },
}


@dataclass(frozen=True)
class LifecycleUpdate:
    """The intentionally small subset of an Event used by the local PoC."""

    event_id: str
    event_type: str
    created: int
    resource_id: str | None
    subscription_id: str | None
    customer_id: str | None
    plan_code: str | None = None
    subscription_status: str | None = None
    access_state: str | None = None
    checkout_session_id: str | None = None
    invoice_id: str | None = None
    invoice_status: str | None = None
    amount_due: int | None = None
    amount_paid: int | None = None
    currency: str | None = None
    cancel_at_period_end: bool | None = None
    current_period_end: int | None = None


def verify_event(payload: bytes, signature: str, webhook_secret: str) -> object:
    if not signature:
        raise InputError("Stripe-Signature header is required")
    return stripe.Webhook.construct_event(payload, signature, webhook_secret)


def event_summary(event: object) -> dict[str, Any]:
    event_id = object_value(event, "id")
    event_type = object_value(event, "type")
    created = object_value(event, "created")
    if not isinstance(event_id, str):
        raise InputError("webhook event ID is missing")
    require_stripe_id(event_id, "evt_", "event")
    if not isinstance(event_type, str) or not re.fullmatch(r"[a-z0-9_.]{1,100}", event_type):
        raise InputError("webhook event type is invalid")
    if not isinstance(created, int):
        raise InputError("webhook event timestamp is invalid")
    if object_value(event, "livemode") is not False:
        raise InputError("webhook event must come from Stripe test mode")
    data = object_value(event, "data", {})
    data_object = object_value(data, "object", {})
    resource_id = object_value(data_object, "id")
    if not isinstance(resource_id, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,255}", resource_id):
        resource_id = None
    subscription_id = None
    customer_id = None
    subscription_status = None
    access_state = None
    invoice_status = None
    plan_code = None
    if event_type in HANDLED_EVENT_TYPES:
        customer_id = _customer_reference(data_object, event_type)
        subscription_id = _subscription_reference(data_object, event_type)
        plan_code = _plan_code(data_object, event_type)
        if event_type == "checkout.session.completed":
            access_state = "awaiting_invoice"
        elif event_type == "invoice.paid":
            access_state = "active"
            invoice_status = _safe_status(object_value(data_object, "status"))
        elif event_type == "invoice.payment_failed":
            access_state = "payment_issue"
            invoice_status = _safe_status(object_value(data_object, "status"))
        else:
            subscription_status = (
                "canceled"
                if event_type == "customer.subscription.deleted"
                else _safe_status(object_value(data_object, "status"))
            )
            if event_type != "customer.subscription.created":
                access_state = _access_state(subscription_status)
    else:
        # Observed-only events still retain safe object references when Stripe exposes
        # them. Unknown shapes must not prevent the delivery itself from being recorded.
        try:
            customer_id = _customer_reference(data_object, event_type)
            subscription_id = _subscription_reference(data_object, event_type)
        except InputError:
            customer_id = None
            subscription_id = None
    return {
        "id": event_id,
        "type": event_type,
        "created": created,
        "resource_id": resource_id,
        "handled": event_type in HANDLED_EVENT_TYPES,
        "subscription_id": subscription_id,
        "customer_id": customer_id,
        "subscription_status": subscription_status,
        "access_state": access_state,
        "invoice_status": invoice_status,
        "plan_code": plan_code,
    }


class WebhookEventStore:
    """Concurrency-safe, full-history event ledger keyed by Stripe Event ID."""

    def __init__(self, state_dir: Path, *, lease_seconds: int = 300, clock=time.time) -> None:
        self.path = state_dir / "webhook-events.sqlite3"
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            try:
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            db.execute(
                """CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, created INTEGER NOT NULL,
                resource_id TEXT, handled INTEGER NOT NULL, status TEXT NOT NULL,
                processed_at INTEGER NOT NULL, details TEXT, claimed_at INTEGER,
                claim_token TEXT, received_at INTEGER, completed_at INTEGER,
                subscription_id TEXT, customer_id TEXT, subscription_status TEXT,
                access_state TEXT, invoice_status TEXT, plan_code TEXT
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
            migrations = {
                "claimed_at": "INTEGER",
                "claim_token": "TEXT",
                "received_at": "INTEGER",
                "completed_at": "INTEGER",
                "subscription_id": "TEXT",
                "customer_id": "TEXT",
                "subscription_status": "TEXT",
                "access_state": "TEXT",
                "invoice_status": "TEXT",
                "plan_code": "TEXT",
            }
            for column, column_type in migrations.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE events ADD COLUMN {column} {column_type}")
            db.execute("UPDATE events SET received_at = processed_at WHERE received_at IS NULL")
            db.execute(
                "UPDATE events SET completed_at = processed_at "
                "WHERE completed_at IS NULL AND status = 'processed'"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def claim_with_token(self, summary: dict[str, Any]) -> str | None:
        now = int(self.clock())
        token = str(uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "INSERT OR IGNORE INTO events "
                "(id,type,created,resource_id,handled,status,processed_at,details,claimed_at,"
                "claim_token,received_at,completed_at,subscription_id,customer_id,"
                "subscription_status,access_state,invoice_status,plan_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary["id"],
                    summary["type"],
                    summary["created"],
                    summary.get("resource_id"),
                    int(summary.get("handled", False)),
                    "processing",
                    now,
                    None,
                    now,
                    token,
                    now,
                    None,
                    summary.get("subscription_id"),
                    summary.get("customer_id"),
                    summary.get("subscription_status"),
                    summary.get("access_state"),
                    summary.get("invoice_status"),
                    summary.get("plan_code"),
                ),
            )
            claimed = result.rowcount == 1
            if not claimed:
                result = db.execute(
                    "UPDATE events SET claimed_at = ?, processed_at = ?, claim_token = ? "
                    "WHERE id = ? AND status = 'processing' "
                    "AND (claimed_at IS NULL OR claimed_at <= ?)",
                    (now, now, token, summary["id"], now - self.lease_seconds),
                )
                claimed = result.rowcount == 1
            db.commit()
            return token if claimed else None
        finally:
            db.close()

    def claim(self, summary: dict[str, Any]) -> bool:
        """Compatibility wrapper for callers that only need a one-shot claim."""

        return self.claim_with_token(summary) is not None

    def complete(
        self,
        event_id: str,
        details: dict[str, Any] | None = None,
        *,
        claim_token: str | None = None,
    ) -> bool:
        predicate = "id = ? AND status = 'processing'"
        with self._connect() as db:
            row = db.execute("SELECT type FROM events WHERE id = ?", (event_id,)).fetchone()
            event_type = row[0] if row is not None else None
            safe_details = _allowlisted_event_details(event_type, details)
            now = int(self.clock())
            values: list[Any] = [
                json.dumps(safe_details, sort_keys=True) if safe_details else None,
                now,
                now,
                event_id,
            ]
            if claim_token is not None:
                predicate += " AND claim_token = ?"
                values.append(claim_token)
            result = db.execute(
                "UPDATE events SET status = 'processed', details = ?, processed_at = ?, "
                "completed_at = ?, claimed_at = NULL, "
                f"claim_token = NULL WHERE {predicate}",
                values,
            )
        return result.rowcount == 1

    def release(self, event_id: str, *, claim_token: str | None = None) -> bool:
        predicate = "id = ? AND status = 'processing'"
        values = [event_id]
        if claim_token is not None:
            predicate += " AND claim_token = ?"
            values.append(claim_token)
        with self._connect() as db:
            result = db.execute(f"DELETE FROM events WHERE {predicate}", values)
        return result.rowcount == 1

    def record(self, summary: dict[str, Any]) -> bool:
        token = self.claim_with_token(summary)
        if token is not None:
            self.complete(summary["id"], claim_token=token)
        return token is not None

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT id,type,created,resource_id,handled,status,processed_at,details,"
            "received_at,completed_at,subscription_id,customer_id,subscription_status,"
            "access_state,invoice_status,plan_code FROM events "
            "ORDER BY received_at DESC, rowid DESC"
        )
        params: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            details = _stored_details(row[7])
            result.append(
                event_view(
                    {
                        "id": row[0],
                        "type": row[1],
                        "created": row[2],
                        "resource_id": row[3],
                        "handled": bool(row[4]),
                        "status": row[5],
                        "processed_at": row[6],
                        "details": details,
                        "received_at": row[8],
                        "completed_at": row[9],
                        "subscription_id": row[10] or details.get("subscription_id"),
                        "customer_id": row[11],
                        "subscription_status": row[12],
                        "access_state": row[13],
                        "invoice_status": row[14],
                        "plan_code": row[15],
                    }
                )
            )
        return result


class LifecycleStore:
    """Idempotent local projection of the PoC's subscription lifecycle."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "subscription-lifecycle.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            try:
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            db.execute(
                """CREATE TABLE IF NOT EXISTS lifecycle_events (
                event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, created INTEGER NOT NULL,
                resource_id TEXT, subscription_id TEXT, customer_id TEXT,
                plan_code TEXT, subscription_status TEXT, access_state TEXT,
                checkout_session_id TEXT,
                invoice_id TEXT, invoice_status TEXT, amount_due INTEGER, amount_paid INTEGER,
                currency TEXT, cancel_at_period_end INTEGER, current_period_end INTEGER
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS subscriptions (
                subscription_id TEXT PRIMARY KEY, customer_id TEXT, subscription_status TEXT,
                access_state TEXT NOT NULL, checkout_session_id TEXT, latest_invoice_id TEXT,
                latest_invoice_status TEXT, latest_amount_due INTEGER,
                latest_amount_paid INTEGER, currency TEXT, cancel_at_period_end INTEGER,
                current_period_end INTEGER, last_event_id TEXT NOT NULL,
                last_event_type TEXT NOT NULL, last_event_created INTEGER NOT NULL,
                checkout_event_created INTEGER, invoice_event_created INTEGER,
                invoice_event_type TEXT, subscription_event_created INTEGER,
                subscription_event_type TEXT, access_event_id TEXT,
                access_event_type TEXT, access_event_created INTEGER, plan_code TEXT
                )"""
            )
            lifecycle_columns = {
                row[1] for row in db.execute("PRAGMA table_info(lifecycle_events)")
            }
            if "plan_code" not in lifecycle_columns:
                db.execute("ALTER TABLE lifecycle_events ADD COLUMN plan_code TEXT")
            columns = {row[1] for row in db.execute("PRAGMA table_info(subscriptions)")}
            migrations = {
                "checkout_event_created": "INTEGER",
                "invoice_event_created": "INTEGER",
                "invoice_event_type": "TEXT",
                "subscription_event_created": "INTEGER",
                "subscription_event_type": "TEXT",
                "access_event_id": "TEXT",
                "access_event_type": "TEXT",
                "access_event_created": "INTEGER",
                "plan_code": "TEXT",
            }
            for column, column_type in migrations.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE subscriptions ADD COLUMN {column} {column_type}")
            self._rebuild_projections(db)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _rebuild_projections(self, db: sqlite3.Connection) -> None:
        """Repair projections from the privacy-safe event history after semantic upgrades."""

        rows = db.execute("SELECT * FROM lifecycle_events ORDER BY rowid ASC").fetchall()
        db.execute("DELETE FROM subscriptions")
        for row in rows:
            self._advance_subscription(db, _lifecycle_update_from_row(row))

    def apply(self, update: LifecycleUpdate) -> bool:
        """Persist history once and advance the projection without regressing newer state."""

        values = asdict(update)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                """INSERT OR IGNORE INTO lifecycle_events (
                event_id,event_type,created,resource_id,subscription_id,customer_id,
                plan_code,subscription_status,access_state,checkout_session_id,invoice_id,invoice_status,
                amount_due,amount_paid,currency,cancel_at_period_end,current_period_end
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["event_id"],
                    values["event_type"],
                    values["created"],
                    values["resource_id"],
                    values["subscription_id"],
                    values["customer_id"],
                    values["plan_code"],
                    values["subscription_status"],
                    values["access_state"],
                    values["checkout_session_id"],
                    values["invoice_id"],
                    values["invoice_status"],
                    values["amount_due"],
                    values["amount_paid"],
                    values["currency"],
                    _optional_bool_int(values["cancel_at_period_end"]),
                    values["current_period_end"],
                ),
            ).rowcount
            if inserted and update.subscription_id is not None:
                self._advance_subscription(db, update)
            db.commit()
            return inserted == 1
        finally:
            db.close()

    def _advance_subscription(self, db: sqlite3.Connection, update: LifecycleUpdate) -> None:
        current = db.execute(
            "SELECT * FROM subscriptions WHERE subscription_id = ?",
            (update.subscription_id,),
        ).fetchone()
        candidate = (
            dict(current)
            if current is not None
            else {
                "subscription_id": update.subscription_id,
                "customer_id": None,
                "subscription_status": None,
                "access_state": "unknown",
                "checkout_session_id": None,
                "latest_invoice_id": None,
                "latest_invoice_status": None,
                "latest_amount_due": None,
                "latest_amount_paid": None,
                "currency": None,
                "cancel_at_period_end": None,
                "current_period_end": None,
                "last_event_id": update.event_id,
                "last_event_type": update.event_type,
                "last_event_created": update.created,
                "checkout_event_created": None,
                "invoice_event_created": None,
                "invoice_event_type": None,
                "subscription_event_created": None,
                "subscription_event_type": None,
                "access_event_id": None,
                "access_event_type": None,
                "access_event_created": None,
                "plan_code": None,
            }
        )
        if update.customer_id is not None:
            candidate["customer_id"] = update.customer_id
        if update.plan_code is not None:
            candidate["plan_code"] = update.plan_code
        if current is not None and _semantic_event_is_newer(
            update.created,
            update.event_type,
            candidate["last_event_created"],
            candidate["last_event_type"],
        ):
            candidate["last_event_id"] = update.event_id
            candidate["last_event_type"] = update.event_type
            candidate["last_event_created"] = update.created

        if update.event_type == "checkout.session.completed":
            if _semantic_event_is_newer(
                update.created,
                update.event_type,
                candidate["checkout_event_created"],
                "checkout.session.completed",
            ):
                candidate["checkout_session_id"] = update.checkout_session_id
                candidate["checkout_event_created"] = update.created
            if candidate["access_event_type"] in {None, "checkout.session.completed"}:
                candidate["access_state"] = "awaiting_invoice"
                _set_access_source(candidate, update)

        elif update.event_type in {"invoice.paid", "invoice.payment_failed"}:
            if _semantic_event_is_newer(
                update.created,
                update.event_type,
                candidate["invoice_event_created"],
                candidate["invoice_event_type"],
            ):
                candidate["latest_invoice_id"] = update.invoice_id
                candidate["latest_invoice_status"] = update.invoice_status
                candidate["latest_amount_due"] = update.amount_due
                candidate["latest_amount_paid"] = update.amount_paid
                candidate["currency"] = update.currency
                candidate["invoice_event_created"] = update.created
                candidate["invoice_event_type"] = update.event_type
                subscription_is_terminal = (
                    candidate["subscription_event_type"] == "customer.subscription.deleted"
                    or candidate["subscription_status"] in _TERMINAL_SUBSCRIPTION_STATUSES
                )
                access_source_is_newer_subscription = (
                    candidate["access_event_type"] == "customer.subscription.updated"
                    and candidate["access_event_created"] is not None
                    and candidate["access_event_created"] > update.created
                )
                if not subscription_is_terminal and not access_source_is_newer_subscription:
                    candidate["access_state"] = update.access_state or candidate["access_state"]
                    _set_access_source(candidate, update)

        elif update.event_type == "customer.subscription.deleted":
            # A deleted Subscription ID cannot become active again. A new Checkout creates a
            # new Subscription ID, so later invoice delivery for this ID must not restore access.
            candidate["subscription_status"] = "canceled"
            candidate["cancel_at_period_end"] = _optional_bool_int(update.cancel_at_period_end)
            candidate["current_period_end"] = update.current_period_end
            candidate["subscription_event_created"] = update.created
            candidate["subscription_event_type"] = update.event_type
            candidate["access_state"] = "inactive"
            _set_access_source(candidate, update)

        elif update.event_type in {
            "customer.subscription.created",
            "customer.subscription.updated",
        }:
            terminal = candidate["subscription_event_type"] == "customer.subscription.deleted"
            newer_subscription = _semantic_event_is_newer(
                update.created,
                update.event_type,
                candidate["subscription_event_created"],
                candidate["subscription_event_type"],
            )
            if not terminal and newer_subscription:
                candidate["subscription_status"] = update.subscription_status
                candidate["cancel_at_period_end"] = _optional_bool_int(update.cancel_at_period_end)
                candidate["current_period_end"] = update.current_period_end
                candidate["subscription_event_created"] = update.created
                candidate["subscription_event_type"] = update.event_type
                if update.event_type == "customer.subscription.updated":
                    access_source_is_newer_invoice = (
                        candidate["access_event_type"] in {"invoice.paid", "invoice.payment_failed"}
                        and candidate["access_event_created"] is not None
                        and candidate["access_event_created"] > update.created
                    )
                    if not access_source_is_newer_invoice:
                        candidate["access_state"] = update.access_state or candidate["access_state"]
                        _set_access_source(candidate, update)
        db.execute(
            """INSERT OR REPLACE INTO subscriptions (
            subscription_id,customer_id,subscription_status,access_state,checkout_session_id,
            latest_invoice_id,latest_invoice_status,latest_amount_due,latest_amount_paid,currency,
            cancel_at_period_end,current_period_end,last_event_id,last_event_type,last_event_created,
            checkout_event_created,invoice_event_created,invoice_event_type,
            subscription_event_created,subscription_event_type,access_event_id,access_event_type,
            access_event_created,plan_code
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(
                candidate[key]
                for key in (
                    "subscription_id",
                    "customer_id",
                    "subscription_status",
                    "access_state",
                    "checkout_session_id",
                    "latest_invoice_id",
                    "latest_invoice_status",
                    "latest_amount_due",
                    "latest_amount_paid",
                    "currency",
                    "cancel_at_period_end",
                    "current_period_end",
                    "last_event_id",
                    "last_event_type",
                    "last_event_created",
                    "checkout_event_created",
                    "invoice_event_created",
                    "invoice_event_type",
                    "subscription_event_created",
                    "subscription_event_type",
                    "access_event_id",
                    "access_event_type",
                    "access_event_created",
                    "plan_code",
                )
            ),
        )

    def subscriptions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM subscriptions ORDER BY last_event_created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_public_lifecycle_row(row) for row in rows]

    def plan_b_subscriptions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return opaque IDs learned from verified Plan B lifecycle events."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM subscriptions WHERE plan_code=? "
                "ORDER BY last_event_created DESC LIMIT ?",
                (product.PLAN_B_CODE, limit),
            ).fetchall()
        return [_public_lifecycle_row(row) for row in rows]

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM lifecycle_events ORDER BY created DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_public_lifecycle_row(row) for row in rows]


def lifecycle_update(event: object) -> LifecycleUpdate | None:
    """Extract an allowlisted lifecycle change from a verified snapshot Event."""

    summary = event_summary(event)
    event_type = summary["type"]
    if event_type not in HANDLED_EVENT_TYPES:
        return None
    data_object = object_value(object_value(event, "data", {}), "object", {})
    customer_id = _stripe_reference(data_object, "customer", "cus_", "customer")
    subscription_id = _subscription_reference(data_object, event_type)
    common = {
        "event_id": summary["id"],
        "event_type": event_type,
        "created": summary["created"],
        "resource_id": summary["resource_id"],
        "subscription_id": subscription_id,
        "customer_id": customer_id,
        "plan_code": _plan_code(data_object, event_type),
    }
    if event_type == "checkout.session.completed":
        return LifecycleUpdate(
            **common,
            access_state="awaiting_invoice",
            checkout_session_id=_stripe_resource(data_object, "cs_", "Checkout Session"),
        )
    if event_type in {"invoice.paid", "invoice.payment_failed"}:
        invoice_paid = event_type == "invoice.paid"
        return LifecycleUpdate(
            **common,
            access_state="active" if invoice_paid else "payment_issue",
            invoice_id=_stripe_resource(data_object, "in_", "Invoice"),
            invoice_status=_safe_status(object_value(data_object, "status")),
            amount_due=_safe_nonnegative_int(object_value(data_object, "amount_due")),
            amount_paid=_safe_nonnegative_int(object_value(data_object, "amount_paid")),
            currency=_safe_currency(object_value(data_object, "currency")),
        )
    status = (
        "canceled"
        if event_type == "customer.subscription.deleted"
        else _safe_status(object_value(data_object, "status"))
    )
    common["subscription_id"] = _stripe_resource(data_object, "sub_", "Subscription")
    return LifecycleUpdate(
        **common,
        subscription_status=status,
        access_state=(
            None if event_type == "customer.subscription.created" else _access_state(status)
        ),
        cancel_at_period_end=_safe_optional_bool(object_value(data_object, "cancel_at_period_end")),
        current_period_end=_safe_nonnegative_int(object_value(data_object, "current_period_end")),
    )


def thin_event_summary(notification: object) -> dict[str, Any]:
    event_id = object_value(notification, "id")
    event_type = object_value(notification, "type")
    created = object_value(notification, "created")
    require_stripe_id(event_id, "evt_", "event")
    if event_type not in THIN_EVENT_TYPES:
        raise InputError("thin webhook event type is not supported")
    if not isinstance(created, (int, str)):
        raise InputError("thin webhook event timestamp is invalid")
    if object_value(notification, "livemode") is not False:
        raise InputError("thin webhook event must come from Stripe test mode")
    return {
        "id": event_id,
        "type": event_type,
        "created": created,
        "resource_id": None,
        "handled": True,
        "subscription_id": None,
        "customer_id": None,
        "subscription_status": None,
        "access_state": None,
        "invoice_status": None,
        "plan_code": None,
    }


def event_view(row: dict[str, Any]) -> dict[str, Any]:
    """Return privacy-safe copy and timing for one unique verified webhook event."""

    result = dict(row)
    event_type = str(result.get("type") or "unknown")
    copy = _EVENT_COPY.get(event_type)
    if copy is None:
        copy = {
            "title": event_type,
            "explanation": f"Stripe sent a verified {event_type} webhook event.",
            "application_action": "No application action is configured for this event type.",
            "entitlement_impact": "No subscription access change.",
            "next_step": "Review the event type before deciding whether to add a handler.",
        }
    result.update(copy)
    result["event_id"] = result.get("id")
    result["event_type"] = event_type
    result["verification_status"] = "Verified"
    result["processing_status"] = str(result.get("status") or "unknown").capitalize()

    details = result.get("details")
    if not isinstance(details, dict):
        details = {}
    result["details"] = details
    matched = details.get("matched_usage_updates")
    if event_type in THIN_EVENT_TYPES and isinstance(matched, int):
        if matched:
            result["application_action"] += (
                f" {matched} matching local usage update"
                f"{'s were' if matched != 1 else ' was'} marked as rejected."
            )
        else:
            result["application_action"] += " No matching local usage update was found."
    elif event_type in HANDLED_EVENT_TYPES and details.get("lifecycle_updated") is False:
        result["application_action"] += " No new local lifecycle record was needed."

    created_display, created_iso = _timestamp_copy(result.get("created"))
    processed_at = result.get("completed_at") or result.get("processed_at")
    processed_display, processed_iso = _timestamp_copy(processed_at)
    received_display, received_iso = _timestamp_copy(result.get("received_at"))
    result.update(
        {
            "created_display": created_display,
            "created_iso": created_iso,
            "processed_display": processed_display,
            "processed_iso": processed_iso,
            "received_display": received_display,
            "received_iso": received_iso,
            "object_relations": _object_relations(result),
        }
    )
    return result


def _object_relations(row: dict[str, Any]) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(object_type: str, object_id: object) -> None:
        if isinstance(object_id, str) and object_id not in seen:
            seen.add(object_id)
            relations.append({"object_type": object_type, "id": object_id})

    resource_id = row.get("resource_id")
    if isinstance(resource_id, str):
        object_type = {
            "cs_": "Checkout Session",
            "in_": "Invoice",
            "sub_": "Subscription",
        }.get(resource_id.split("test_")[0] if "test_" in resource_id else resource_id[:3])
        if object_type is None:
            if resource_id.startswith("cs_"):
                object_type = "Checkout Session"
            elif resource_id.startswith("in_"):
                object_type = "Invoice"
            elif resource_id.startswith("sub_"):
                object_type = "Subscription"
            else:
                object_type = "Stripe object"
        add(object_type, resource_id)
    add("Invoice", row.get("invoice_id"))
    add("Subscription", row.get("subscription_id"))
    add("Customer", row.get("customer_id"))
    return relations


def _timestamp_copy(value: object) -> tuple[str | None, str | None]:
    if isinstance(value, int):
        parsed = datetime.fromtimestamp(value, UTC)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value, None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
    else:
        return None, None
    return (
        parsed.strftime("%Y-%m-%d %H:%M:%S UTC"),
        parsed.isoformat().replace("+00:00", "Z"),
    )


def _allowlisted_event_details(
    event_type: object, details: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    result: dict[str, Any] = {}
    if event_type in HANDLED_EVENT_TYPES:
        updated = details.get("lifecycle_updated")
        if isinstance(updated, bool):
            result["lifecycle_updated"] = updated
        subscription_id = details.get("subscription_id")
        if isinstance(subscription_id, str):
            result["subscription_id"] = require_stripe_id(subscription_id, "sub_", "subscription")
    if event_type in THIN_EVENT_TYPES:
        matched = details.get("matched_usage_updates")
        if isinstance(matched, int) and not isinstance(matched, bool) and matched >= 0:
            result["matched_usage_updates"] = matched
    return result


def _stored_details(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _plan_code(obj: object, event_type: str) -> str | None:
    metadata_sources = [object_value(obj, "metadata", {})]
    if event_type.startswith("invoice."):
        parent = object_value(obj, "parent", {})
        metadata_sources.extend(
            [
                object_value(object_value(parent, "subscription_details", {}), "metadata", {}),
                object_value(object_value(obj, "subscription_details", {}), "metadata", {}),
            ]
        )
    found: set[str] = set()
    for metadata in metadata_sources:
        value = object_value(metadata, product.PLAN_METADATA_KEY)
        if value is None:
            continue
        if not isinstance(value, str):
            raise InputError("webhook plan metadata is invalid")
        found.add(value)
    if not found:
        return None
    if len(found) != 1:
        raise InputError("webhook plan metadata conflicts")
    value = found.pop()
    if value not in {product.PLAN_A_CODE, product.PLAN_B_CODE}:
        raise InputError("webhook plan metadata is invalid")
    return value


def _stripe_reference(obj: object, key: str, prefix: str, label: str) -> str | None:
    value = object_value(obj, key)
    if value is None:
        return None
    candidate = value if isinstance(value, str) else object_value(value, "id")
    if not isinstance(candidate, str):
        raise InputError(f"webhook {label} reference is invalid")
    return require_stripe_id(candidate, prefix, label)


def _stripe_resource(obj: object, prefix: str, label: str) -> str:
    value = object_value(obj, "id")
    if not isinstance(value, str):
        raise InputError(f"webhook {label} ID is missing")
    return require_stripe_id(value, prefix, label)


def _customer_reference(obj: object, event_type: str) -> str | None:
    if event_type in {"customer.created", "customer.updated", "customer.deleted"}:
        return _stripe_resource(obj, "cus_", "Customer")
    return _stripe_reference(obj, "customer", "cus_", "customer")


def _subscription_reference(obj: object, event_type: str) -> str | None:
    if event_type.startswith("customer.subscription."):
        return _stripe_resource(obj, "sub_", "Subscription")
    direct = _stripe_reference(obj, "subscription", "sub_", "subscription")
    if direct is not None:
        return direct
    parent = object_value(obj, "parent", {})
    details = object_value(parent, "subscription_details", {})
    return _stripe_reference(details, "subscription", "sub_", "subscription")


def _safe_status(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAFE_STATUS.fullmatch(value):
        raise InputError("webhook status is invalid")
    return value


def _safe_currency(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAFE_CURRENCY.fullmatch(value):
        raise InputError("webhook currency is invalid")
    return value


def _safe_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputError("webhook amount or timestamp is invalid")
    return value


def _safe_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise InputError("webhook boolean is invalid")
    return value


def _access_state(status: str | None) -> str:
    if status in {"active", "trialing"}:
        return "active"
    if status in {"past_due", "unpaid", "incomplete"}:
        return "payment_issue"
    if status in {"canceled", "incomplete_expired", "paused"}:
        return "inactive"
    return "unknown"


def _optional_bool_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _semantic_event_is_newer(
    created: int,
    event_type: str,
    current_created: int | None,
    current_type: str | None,
) -> bool:
    if current_created is None:
        return True
    if created != current_created:
        return created > current_created
    return _LIFECYCLE_EVENT_PRECEDENCE.get(event_type, 0) >= _LIFECYCLE_EVENT_PRECEDENCE.get(
        current_type or "", 0
    )


def _set_access_source(candidate: dict[str, Any], update: LifecycleUpdate) -> None:
    candidate["access_event_id"] = update.event_id
    candidate["access_event_type"] = update.event_type
    candidate["access_event_created"] = update.created


def _lifecycle_update_from_row(row: sqlite3.Row) -> LifecycleUpdate:
    cancel_at_period_end = row["cancel_at_period_end"]
    return LifecycleUpdate(
        event_id=row["event_id"],
        event_type=row["event_type"],
        created=row["created"],
        resource_id=row["resource_id"],
        subscription_id=row["subscription_id"],
        customer_id=row["customer_id"],
        plan_code=row["plan_code"],
        subscription_status=row["subscription_status"],
        access_state=row["access_state"],
        checkout_session_id=row["checkout_session_id"],
        invoice_id=row["invoice_id"],
        invoice_status=row["invoice_status"],
        amount_due=row["amount_due"],
        amount_paid=row["amount_paid"],
        currency=row["currency"],
        cancel_at_period_end=(None if cancel_at_period_end is None else bool(cancel_at_period_end)),
        current_period_end=row["current_period_end"],
    )


def _public_lifecycle_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    if "cancel_at_period_end" in result and result["cancel_at_period_end"] is not None:
        result["cancel_at_period_end"] = bool(result["cancel_at_period_end"])
    return result
