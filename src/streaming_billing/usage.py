from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from .pricing import METER_EVENT_NAME, excess_gb, overage_blocks, plan_b_total_cents
from .state import Catalog
from .stripe_utils import object_value, require_test_object
from .validation import InputError, require_event_id, require_stripe_id


@dataclass(frozen=True)
class UsageUpdate:
    subscription_id: str
    subscription_item_id: str
    customer_id: str
    period_start: int
    period_end: int
    revision: int
    event_id: str
    total_gb: Decimal

    def validated(self) -> UsageUpdate:
        if not all(
            isinstance(v, str)
            for v in (self.subscription_id, self.subscription_item_id, self.customer_id)
        ):
            raise InputError("Stripe Subscription identifiers are missing")
        require_stripe_id(self.subscription_id, "sub_", "subscription")
        require_stripe_id(self.subscription_item_id, "si_", "subscription item")
        require_stripe_id(self.customer_id, "cus_", "customer")
        require_event_id(self.event_id)
        if (
            not isinstance(self.period_start, int)
            or not isinstance(self.period_end, int)
            or self.period_start < 1
            or self.period_end <= self.period_start
        ):
            raise InputError("subscription item billing period is invalid")
        if self.revision < 1:
            raise InputError("revision must be a positive integer")
        if not self.total_gb.is_finite() or self.total_gb < 0:
            raise InputError("total_gb must be finite and non-negative")
        return self


@dataclass(frozen=True)
class UsageContext:
    subscription_id: str
    subscription_item_id: str
    customer_id: str
    period_start: int
    period_end: int

    def update(self, revision: int, event_id: str, total_gb: Decimal) -> UsageUpdate:
        return UsageUpdate(
            self.subscription_id,
            self.subscription_item_id,
            self.customer_id,
            self.period_start,
            self.period_end,
            revision,
            event_id,
            total_gb,
        ).validated()


def usage_update_from_subscription(
    client: Any,
    catalog: Catalog,
    subscription_id: str,
    revision: int,
    event_id: str,
    total_gb: Decimal,
) -> UsageUpdate:
    context = usage_context_from_subscription(client, catalog, subscription_id)
    return context.update(revision, event_id, total_gb)


def usage_context_from_subscription(
    client: Any,
    catalog: Catalog,
    subscription_id: str,
) -> UsageContext:
    """Resolve and validate the Plan B billing target directly against Stripe."""

    require_stripe_id(subscription_id, "sub_", "subscription")
    subscription = client.v1.subscriptions.retrieve(
        subscription_id, {"expand": ["items.data.price"]}
    )
    require_test_object(subscription, "Subscription")
    if object_value(subscription, "id") != subscription_id:
        raise RuntimeError("Stripe returned a different Subscription.")
    if object_value(subscription, "status") not in {"active", "trialing"}:
        raise InputError("subscription must be active or trialing")
    customer_id = object_value(subscription, "customer")
    if not isinstance(customer_id, str):
        customer_id = object_value(customer_id, "id")
    items = object_value(object_value(subscription, "items", {}), "data", [])
    matches = []
    for item in items if isinstance(items, list) else []:
        price = object_value(item, "price", {})
        price_id = price if isinstance(price, str) else object_value(price, "id")
        if price_id == catalog.plan_b_overage_price:
            matches.append((item, price))
    if len(matches) != 1:
        raise InputError("subscription must contain the configured Plan B metered Price")
    item, price = matches[0]
    recurring = object_value(price, "recurring", {})
    meter = object_value(recurring, "meter")
    if not isinstance(meter, str):
        meter = object_value(meter, "id")
    if object_value(recurring, "usage_type") != "metered" or meter != catalog.plan_b_meter:
        raise RuntimeError("The Plan B overage Price is not attached to its configured Meter.")
    context = UsageContext(
        subscription_id,
        object_value(item, "id"),
        customer_id,
        object_value(item, "current_period_start"),
        object_value(item, "current_period_end"),
    )
    # Reuse the existing strict validation without inventing a durable identity.
    context.update(1, "server-validation", Decimal(0))
    return context


class UsageLedger:
    """Transactional usage revisions and per-period network submission leases."""

    def __init__(self, state_dir: Path, *, lease_seconds: int = 300, clock=time.time) -> None:
        self.path = state_dir / "usage-ledger.sqlite3"
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            try:
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            db.execute("""CREATE TABLE IF NOT EXISTS periods (
                period_key TEXT PRIMARY KEY, latest_revision INTEGER NOT NULL,
                active_event_id TEXT, lease_token TEXT, lease_until INTEGER
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS updates (
                event_id TEXT PRIMARY KEY, period_key TEXT NOT NULL, revision INTEGER NOT NULL,
                subscription_id TEXT NOT NULL, subscription_item_id TEXT NOT NULL,
                customer_id TEXT NOT NULL, period_start INTEGER NOT NULL,
                period_end INTEGER NOT NULL,
                total_gb TEXT NOT NULL, reported_excess_gb INTEGER NOT NULL,
                payload_hash TEXT NOT NULL, status TEXT NOT NULL, submitted_at INTEGER,
                error_event_id TEXT, errors TEXT,
                UNIQUE(period_key, revision)
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def prepare_update(self, context: UsageContext, total_gb: Decimal) -> UsageUpdate:
        """Allocate or recover a server-owned retry identity for a minimal form request."""

        # Validate context and submitted input before opening the write transaction.
        context.update(1, "server-validation", total_gb)
        period_key = f"{context.subscription_item_id}:{context.period_start}:{context.period_end}"
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT * FROM updates WHERE period_key=? ORDER BY revision DESC LIMIT 1",
                (period_key,),
            ).fetchone()
            if latest is not None and Decimal(latest["total_gb"]) == total_gb:
                if latest["status"] != "rejected":
                    db.commit()
                    return _update_from_row(latest)
            if latest is not None and latest["status"] == "pending":
                raise InputError(
                    "A previous usage request is awaiting confirmation. Retry its total unchanged."
                )
            revision = int(latest["revision"] if latest is not None else 0) + 1
            event_id = f"usage-{uuid4()}"
            update = context.update(revision, event_id, total_gb)
            db.execute(
                "INSERT INTO updates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    update.event_id,
                    period_key,
                    update.revision,
                    update.subscription_id,
                    update.subscription_item_id,
                    update.customer_id,
                    update.period_start,
                    update.period_end,
                    str(update.total_gb),
                    excess_gb(update.total_gb),
                    _payload_hash(update),
                    "pending",
                    None,
                    None,
                    None,
                ),
            )
            db.execute(
                "INSERT INTO periods VALUES (?,?,?,?,?) ON CONFLICT(period_key) DO UPDATE SET "
                "latest_revision=MAX(latest_revision, excluded.latest_revision)",
                (period_key, revision, None, None, None),
            )
            db.commit()
            return update
        finally:
            db.close()

    def claim_submission(self, update: UsageUpdate) -> tuple[str, str | None]:
        update.validated()
        now = int(self.clock())
        key = _period_key(update)
        digest = _payload_hash(update)
        token = str(uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            period = db.execute("SELECT * FROM periods WHERE period_key = ?", (key,)).fetchone()
            existing_id = db.execute(
                "SELECT * FROM updates WHERE event_id = ?", (update.event_id,)
            ).fetchone()
            existing_revision = db.execute(
                "SELECT * FROM updates WHERE period_key = ? AND revision = ?",
                (key, update.revision),
            ).fetchone()
            existing = existing_id or existing_revision
            if existing:
                if existing["payload_hash"] != digest or existing["event_id"] != update.event_id:
                    raise InputError("the saved event ID or revision has different usage data")
                if existing["status"] in {"submitted", "confirmed", "rejected"}:
                    db.commit()
                    return existing["status"], None
            if period and period["active_event_id"] and period["lease_until"] > now:
                db.rollback()
                return "in_flight", None
            if period and update.revision < period["latest_revision"]:
                raise InputError("revision is older than the saved usage revision")
            if existing is None:
                db.execute(
                    "INSERT INTO updates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        update.event_id,
                        key,
                        update.revision,
                        update.subscription_id,
                        update.subscription_item_id,
                        update.customer_id,
                        update.period_start,
                        update.period_end,
                        str(update.total_gb),
                        excess_gb(update.total_gb),
                        digest,
                        "pending",
                        None,
                        None,
                        None,
                    ),
                )
            db.execute(
                "INSERT INTO periods VALUES (?,?,?,?,?) ON CONFLICT(period_key) DO UPDATE SET "
                "latest_revision = MAX(latest_revision, excluded.latest_revision), "
                "active_event_id=excluded.active_event_id, lease_token=excluded.lease_token, "
                "lease_until=excluded.lease_until",
                (key, update.revision, update.event_id, token, now + self.lease_seconds),
            )
            db.commit()
            return "claimed", token
        finally:
            db.close()

    def mark_submitted(
        self,
        update: UsageUpdate,
        token: str,
        *,
        submitted_at: int | None = None,
    ) -> int:
        submitted_at = int(self.clock()) if submitted_at is None else submitted_at
        key = _period_key(update)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE updates SET status='submitted', submitted_at=? WHERE event_id=? "
                "AND payload_hash=? AND status='pending'",
                (submitted_at, update.event_id, _payload_hash(update)),
            )
            released = db.execute(
                "UPDATE periods SET active_event_id=NULL, lease_token=NULL, lease_until=NULL "
                "WHERE period_key=? AND active_event_id=? AND lease_token=?",
                (key, update.event_id, token),
            )
            if result.rowcount != 1 or released.rowcount != 1:
                raise RuntimeError("Usage submission lease changed before completion.")
            db.commit()
            return submitted_at
        finally:
            db.close()

    def release_submission(self, update: UsageUpdate, token: str) -> bool:
        """Release a failed network attempt while retaining its durable retry identity."""

        with self._connect() as db:
            result = db.execute(
                "UPDATE periods SET active_event_id=NULL, lease_token=NULL, lease_until=NULL "
                "WHERE period_key=? AND active_event_id=? AND lease_token=?",
                (_period_key(update), update.event_id, token),
            )
        return result.rowcount == 1

    def mark_errors(
        self, errors_by_identifier: dict[str, list[dict[str, str]]], event_id: str
    ) -> int:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            changed = 0
            for identifier, errors in errors_by_identifier.items():
                changed += db.execute(
                    "UPDATE updates SET status='rejected', error_event_id=?, errors=? "
                    "WHERE event_id=?",
                    (event_id, json.dumps(errors, sort_keys=True), identifier),
                ).rowcount
            db.commit()
            return changed
        finally:
            db.close()

    def latest_for_subscription(self, subscription_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM updates WHERE subscription_id=? "
                "ORDER BY period_start DESC, revision DESC LIMIT 1",
                (subscription_id,),
            ).fetchone()
        if row is None:
            raise InputError("no local usage revision exists for this subscription")
        return dict(row)

    def latest_public_for_subscription(self, subscription_id: str) -> dict[str, Any]:
        require_stripe_id(subscription_id, "sub_", "subscription")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM updates WHERE subscription_id=? "
                "ORDER BY period_start DESC, revision DESC LIMIT 1",
                (subscription_id,),
            ).fetchone()
        if row is None:
            raise InputError("no local usage revision exists for this subscription")
        return _public_update(row)

    def next_revision(self, subscription_id: str) -> int:
        require_stripe_id(subscription_id, "sub_", "subscription")
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(revision) FROM updates WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
        return int(row[0] or 0) + 1

    def by_event(self, event_id: str) -> dict[str, Any] | None:
        require_event_id(event_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM updates WHERE event_id=?", (event_id,)).fetchone()
        return _public_update(row) if row is not None else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM updates ORDER BY COALESCE(submitted_at, 0) DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_public_update(row) for row in rows]


def report_usage(client: Any, ledger: UsageLedger, update: UsageUpdate) -> dict[str, Any]:
    state, token = ledger.claim_submission(update)
    if state in {"submitted", "confirmed"}:
        return _result(update, f"already_{state}")
    if state == "rejected":
        raise InputError("this usage revision was rejected asynchronously by Stripe")
    if state == "in_flight":
        raise InputError("another revision for this billing period is currently being submitted")
    params = {
        "event_name": METER_EVENT_NAME,
        "payload": {
            "stripe_customer_id": update.customer_id,
            "value": str(excess_gb(update.total_gb)),
        },
        "identifier": update.event_id,
    }
    digest = hashlib.sha256(update.event_id.encode()).hexdigest()
    try:
        event = client.v1.billing.meter_events.create(
            params, {"idempotency_key": f"streaming-meter-event-{digest}"}
        )
        require_test_object(event, "Meter Event")
        if object_value(event, "identifier") != update.event_id:
            raise RuntimeError("Stripe returned a different Meter Event identifier.")
        event_timestamp = object_value(event, "timestamp")
        if not isinstance(event_timestamp, int) or event_timestamp < 1:
            event_timestamp = int(ledger.clock())
        ledger.mark_submitted(update, token or "", submitted_at=event_timestamp)
    except Exception:
        ledger.release_submission(update, token or "")
        raise
    return _result(update, "submitted")


def meter_error_details(event: object) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    reason = object_value(object_value(event, "data", {}), "reason", {})
    for error_type in object_value(reason, "error_types", []) or []:
        code = str(object_value(error_type, "code", "meter_event_error"))
        for sample in object_value(error_type, "sample_errors", []) or []:
            identifier = object_value(object_value(sample, "request", {}), "identifier")
            if isinstance(identifier, str):
                result.setdefault(identifier, []).append(
                    {
                        "code": code,
                        "message": str(
                            object_value(sample, "error_message", "Meter event rejected")
                        ),
                    }
                )
    return result


def reconcile_usage(
    client: Any,
    catalog: Catalog,
    ledger: UsageLedger,
    subscription_id: str,
    wait_seconds: int = 0,
    *,
    clock=time.time,
    sleeper=time.sleep,
) -> dict[str, Any]:
    require_stripe_id(subscription_id, "sub_", "subscription")
    if wait_seconds < 0 or wait_seconds > 60:
        raise InputError("wait_seconds must be between 0 and 60")
    entry = ledger.latest_for_subscription(subscription_id)
    if entry["status"] == "rejected":
        return {"status": "rejected", "event_id": entry["event_id"]}
    deadline = clock() + wait_seconds
    while True:
        now = int(clock())
        submitted_at = entry["submitted_at"]
        query_start = (submitted_at // 60) * 60 if submitted_at is not None else 0
        query_end = (min(now, entry["period_end"]) // 60) * 60
        eligible = (
            submitted_at is not None
            and query_start < query_end
            and query_start <= submitted_at < query_end
        )
        if eligible:
            summaries = client.v1.billing.meters.event_summaries.list(
                catalog.plan_b_meter,
                {
                    "customer": entry["customer_id"],
                    "start_time": query_start,
                    "end_time": query_end,
                    "limit": 1,
                },
            )
            data = object_value(summaries, "data", []) or []
            aggregate = object_value(data[0], "aggregated_value") if data else None
            try:
                matched = Decimal(str(aggregate)) == Decimal(str(entry["reported_excess_gb"]))
            except InvalidOperation:
                matched = False
            if matched:
                return {
                    "status": "confirmed",
                    "scope": "period_aggregate",
                    "event_id": entry["event_id"],
                    "aggregated_excess_gb": aggregate,
                }
        else:
            aggregate = None
        if clock() >= deadline:
            return {
                "status": "unavailable" if not eligible else "pending",
                "scope": "period_aggregate",
                "event_id": entry["event_id"],
                "aggregated_excess_gb": aggregate,
            }
        sleeper(min(2, max(0, deadline - clock())))


def _period_key(update: UsageUpdate) -> str:
    return f"{update.subscription_item_id}:{update.period_start}:{update.period_end}"


def _payload_hash(update: UsageUpdate) -> str:
    payload = {
        "period_key": _period_key(update),
        "subscription_id": update.subscription_id,
        "customer_id": update.customer_id,
        "revision": update.revision,
        "event_id": update.event_id,
        "total_gb": str(update.total_gb),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _identity(update: UsageUpdate) -> dict[str, Any]:
    return {
        "subscription_id": update.subscription_id,
        "subscription_item_id": update.subscription_item_id,
        "customer_id": update.customer_id,
        "period_start": update.period_start,
        "period_end": update.period_end,
    }


def _result(update: UsageUpdate, status: str) -> dict[str, Any]:
    return {
        "status": status,
        **_identity(update),
        "revision": update.revision,
        "event_id": update.event_id,
        "total_gb": str(update.total_gb),
        "reported_excess_gb": excess_gb(update.total_gb),
        "billable_10gb_blocks": overage_blocks(update.total_gb),
        "projected_pre_tax_cents": plan_b_total_cents(update.total_gb),
    }


def _update_from_row(row: sqlite3.Row) -> UsageUpdate:
    return UsageUpdate(
        subscription_id=row["subscription_id"],
        subscription_item_id=row["subscription_item_id"],
        customer_id=row["customer_id"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        revision=row["revision"],
        event_id=row["event_id"],
        total_gb=Decimal(row["total_gb"]),
    ).validated()


def _public_update(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    try:
        total_gb = Decimal(result["total_gb"])
    except (InvalidOperation, TypeError):
        total_gb = Decimal(0)
    result["projected_pre_tax_cents"] = plan_b_total_cents(total_gb)
    result["billable_10gb_blocks"] = overage_blocks(total_gb)
    try:
        errors = json.loads(result["errors"]) if result["errors"] else []
    except json.JSONDecodeError:
        errors = [{"code": "local_state_error", "message": "Saved error details are invalid."}]
    result["errors"] = errors if isinstance(errors, list) else []
    return result
