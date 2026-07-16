from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .validation import InputError, require_coupon_id, require_stripe_id

CATALOG_FIELDS = {
    "plan_a_product": "prod_",
    "plan_a_price": "price_",
    "plan_b_base_product": "prod_",
    "plan_b_base_price": "price_",
    "plan_b_overage_product": "prod_",
    "plan_b_meter": "mtr_",
    "plan_b_overage_price": "price_",
    "launch_coupon": None,
    "launch_promotion_code": "promo_",
}


@dataclass(frozen=True)
class Catalog:
    plan_a_product: str
    plan_a_price: str
    plan_b_base_product: str
    plan_b_base_price: str
    plan_b_overage_product: str
    plan_b_meter: str
    plan_b_overage_price: str
    launch_coupon: str
    launch_promotion_code: str


class JsonState:
    def __init__(self, path: Path, schema: str) -> None:
        self.path = path
        self.schema = schema

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema": self.schema}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read local state file {self.path}.") from exc
        if not isinstance(data, dict) or data.get("schema") != self.schema:
            raise RuntimeError(f"Local state file {self.path} has an unsupported schema.")
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)


class CatalogStore:
    def __init__(self, state_dir: Path) -> None:
        self.state = JsonState(state_dir / "catalog.json", "stripe-streaming-catalog-v1")

    @property
    def path(self) -> Path:
        return self.state.path

    def resources(self) -> dict[str, str]:
        data = self.state.load()
        resources = data.get("resources", {})
        if not isinstance(resources, dict):
            raise RuntimeError("Catalog state resources must be an object.")
        checked: dict[str, str] = {}
        for name, value in resources.items():
            if name not in CATALOG_FIELDS or not isinstance(value, str):
                raise RuntimeError("Catalog state contains an unknown resource.")
            checked[name] = self._validate(name, value)
        return checked

    def save_resource(self, name: str, value: str) -> None:
        if name not in CATALOG_FIELDS:
            raise InputError(f"Unknown catalog resource {name}.")
        value = self._validate(name, value)
        data = self.state.load()
        resources = data.setdefault("resources", {})
        if not isinstance(resources, dict):
            raise RuntimeError("Catalog state resources must be an object.")
        existing = resources.get(name)
        if existing and existing != value:
            raise RuntimeError(f"Catalog state already contains a different {name}.")
        resources[name] = value
        self.state.save(data)

    def remove_resources(self, *names: str) -> None:
        """Forget superseded resources after their remote replacement is made safe."""

        if any(name not in CATALOG_FIELDS for name in names):
            raise InputError("Cannot remove an unknown catalog resource.")
        data = self.state.load()
        resources = data.get("resources", {})
        if not isinstance(resources, dict):
            raise RuntimeError("Catalog state resources must be an object.")
        for name in names:
            resources.pop(name, None)
        data["resources"] = resources
        self.state.save(data)

    def account_id(self) -> str | None:
        value = self.state.load().get("account_id")
        if value is None:
            return None
        return require_stripe_id(value, "acct_", "account")

    def instance_nonce(self) -> str:
        data = self.state.load()
        value = data.get("instance_nonce")
        if value is None:
            value = secrets.token_hex(16)
            data["instance_nonce"] = value
            self.state.save(data)
        if not isinstance(value, str) or len(value) != 32:
            raise RuntimeError("Catalog state has an invalid instance nonce.")
        return value

    def save_account_id(self, value: str) -> None:
        value = require_stripe_id(value, "acct_", "account")
        data = self.state.load()
        existing = data.get("account_id")
        if existing and existing != value:
            raise RuntimeError(
                "The local catalog belongs to another Stripe account. Use the matching "
                "test key or move .local aside before creating a separate test catalog."
            )
        data["account_id"] = value
        self.state.save(data)

    def catalog(self) -> Catalog:
        resources = self.resources()
        missing = [name for name in CATALOG_FIELDS if name not in resources]
        if missing:
            raise RuntimeError("Run `streaming-billing setup` to finish the test catalog.")
        return Catalog(**resources)

    def is_complete(self) -> bool:
        try:
            self.catalog()
        except RuntimeError:
            return False
        return True

    @staticmethod
    def _validate(name: str, value: str) -> str:
        prefix = CATALOG_FIELDS[name]
        if prefix is None:
            return require_coupon_id(value)
        return require_stripe_id(value, prefix, name)


def catalog_as_dict(catalog: Catalog) -> dict[str, str]:
    return asdict(catalog)
