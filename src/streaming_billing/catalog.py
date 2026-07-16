from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from . import product_config as product
from .state import Catalog, CatalogStore
from .stripe_utils import object_id, object_value, require_test_object

_IDEMPOTENCY_PREFIX = "streaming-billing-catalog-v2"
_LEGACY_COUPON_NAME = "20% off the first Plan B base invoice"


def provision_catalog(client: Any, store: CatalogStore) -> Catalog:
    """Create missing catalog objects and save each ID before the next request."""

    resources = store.resources()
    instance_nonce = store.instance_nonce()
    _bind_account(client, store)
    _discover_existing(client, store, resources)
    existing_before = set(resources)

    def options(operation: str) -> dict[str, str]:
        return _options(operation, instance_nonce)

    def ensure(name: str, label: str, create: Callable[[], object]) -> str:
        existing = resources.get(name)
        if existing:
            return existing
        result = create()
        require_test_object(result, label)
        result_id = object_id(result, label)
        store.save_resource(name, result_id)
        resources[name] = result_id
        return result_id

    plan_a_product = ensure(
        "plan_a_product",
        "Product",
        lambda: client.v1.products.create(
            {
                "name": product.PLAN_A_NAME,
                "description": product.PLAN_A_DESCRIPTION,
                "metadata": {
                    **product.CATALOG_METADATA,
                    product.PRODUCT_PLAN_METADATA_KEY: product.PLAN_A_CODE,
                },
            },
            options("plan-a-product"),
        ),
    )
    ensure(
        "plan_a_price",
        "Price",
        lambda: client.v1.prices.create(
            {
                "product": plan_a_product,
                "currency": product.CURRENCY,
                "unit_amount": product.PLAN_A_MONTHLY_CENTS,
                "recurring": {
                    "interval": product.BILLING_INTERVAL,
                    "usage_type": "licensed",
                },
                "tax_behavior": product.TAX_BEHAVIOR,
                "lookup_key": product.PLAN_A_LOOKUP_KEY,
                "nickname": product.PLAN_A_PRICE_NICKNAME,
            },
            options("plan-a-price"),
        ),
    )

    plan_b_base_product = ensure(
        "plan_b_base_product",
        "Product",
        lambda: client.v1.products.create(
            {
                "name": product.PLAN_B_BASE_NAME,
                "description": product.plan_b_base_description(),
                "metadata": {
                    **product.CATALOG_METADATA,
                    product.PRODUCT_PLAN_METADATA_KEY: product.PLAN_B_CODE,
                    product.PRODUCT_COMPONENT_METADATA_KEY: product.PLAN_B_BASE_COMPONENT,
                },
            },
            options("plan-b-base-product"),
        ),
    )
    ensure(
        "plan_b_base_price",
        "Price",
        lambda: client.v1.prices.create(
            {
                "product": plan_b_base_product,
                "currency": product.CURRENCY,
                "unit_amount": product.PLAN_B_BASE_MONTHLY_CENTS,
                "recurring": {
                    "interval": product.BILLING_INTERVAL,
                    "usage_type": "licensed",
                },
                "tax_behavior": product.TAX_BEHAVIOR,
                "lookup_key": product.PLAN_B_BASE_LOOKUP_KEY,
                "nickname": product.PLAN_B_BASE_PRICE_NICKNAME,
            },
            options("plan-b-base-price"),
        ),
    )

    plan_b_overage_product = ensure(
        "plan_b_overage_product",
        "Product",
        lambda: client.v1.products.create(
            {
                "name": product.PLAN_B_OVERAGE_NAME,
                "description": product.plan_b_overage_description(),
                "metadata": {
                    **product.CATALOG_METADATA,
                    product.PRODUCT_PLAN_METADATA_KEY: product.PLAN_B_CODE,
                    product.PRODUCT_COMPONENT_METADATA_KEY: product.PLAN_B_OVERAGE_COMPONENT,
                },
            },
            options("plan-b-overage-product"),
        ),
    )
    plan_b_meter = ensure(
        "plan_b_meter",
        "Billing Meter",
        lambda: client.v1.billing.meters.create(
            {
                "display_name": product.METER_DISPLAY_NAME,
                "event_name": product.METER_EVENT_NAME,
                "default_aggregation": {"formula": product.METER_AGGREGATION},
                "customer_mapping": {
                    "type": "by_id",
                    "event_payload_key": product.METER_CUSTOMER_PAYLOAD_KEY,
                },
                "value_settings": {"event_payload_key": product.METER_VALUE_PAYLOAD_KEY},
            },
            options("plan-b-meter"),
        ),
    )
    ensure(
        "plan_b_overage_price",
        "Price",
        lambda: client.v1.prices.create(
            {
                "product": plan_b_overage_product,
                "currency": product.CURRENCY,
                "unit_amount": product.PLAN_B_OVERAGE_PACKAGE_CENTS,
                "recurring": {
                    "interval": product.BILLING_INTERVAL,
                    "usage_type": "metered",
                    "meter": plan_b_meter,
                },
                "transform_quantity": {
                    "divide_by": product.PLAN_B_OVERAGE_PACKAGE_GB,
                    "round": "up",
                },
                "tax_behavior": product.TAX_BEHAVIOR,
                "lookup_key": product.PLAN_B_OVERAGE_LOOKUP_KEY,
                "nickname": product.PLAN_B_OVERAGE_PRICE_NICKNAME,
            },
            options("plan-b-overage-price"),
        ),
    )

    coupon_products = _coupon_products(
        plan_a_product,
        plan_b_base_product,
        plan_b_overage_product,
    )
    _prepare_launch_offer_migration(client, store, resources, coupon_products)

    launch_coupon = ensure(
        "launch_coupon",
        "Coupon",
        lambda: client.v1.coupons.create(
            {
                "name": product.COUPON_NAME,
                "percent_off": product.COUPON_PERCENT_OFF,
                "duration": product.COUPON_DURATION,
                "applies_to": {"products": coupon_products},
                "metadata": product.CATALOG_METADATA,
            },
            options("launch-coupon"),
        ),
    )
    ensure(
        "launch_promotion_code",
        "Promotion Code",
        lambda: client.v1.promotion_codes.create(
            {
                "promotion": {"type": "coupon", "coupon": launch_coupon},
                "code": product.PROMOTION_CODE,
                "metadata": product.CATALOG_METADATA,
            },
            options("launch-promotion-code"),
        ),
    )

    catalog = store.catalog()
    if existing_before:
        _reconcile_existing(client, catalog, existing_before)
    return catalog


def _options(operation: str, instance_nonce: str) -> dict[str, str]:
    return {"idempotency_key": f"{_IDEMPOTENCY_PREFIX}-{instance_nonce}-{operation}"}


def _coupon_products(
    plan_a_product: str, plan_b_base_product: str, plan_b_overage_product: str
) -> list[str]:
    products = {
        "plan_a": plan_a_product,
        "plan_b_base": plan_b_base_product,
        "plan_b_overage": plan_b_overage_product,
    }
    try:
        selected = [products[name] for name in product.COUPON_APPLIES_TO]
    except KeyError as exc:
        raise RuntimeError(
            "COUPON_APPLIES_TO in product_config.py contains an unknown product component."
        ) from exc
    if not selected or any(not item for item in selected) or len(selected) != len(set(selected)):
        raise RuntimeError("COUPON_APPLIES_TO must select distinct configured Products.")
    return selected


def _prepare_launch_offer_migration(
    client: Any,
    store: CatalogStore,
    resources: dict[str, str],
    expected_products: list[str],
) -> None:
    """Replace only the known Plan-B-only launch offer without duplicating its code."""

    coupon_id = resources.get("launch_coupon")
    if coupon_id is None:
        return
    try:
        coupon = client.v1.coupons.retrieve(coupon_id, {"expand": ["applies_to"]})
    except Exception as exc:
        raise RuntimeError("Saved launch_coupon could not be retrieved for migration.") from exc
    if _matches_coupon(coupon, expected_products):
        return
    if not _matches_legacy_coupon(coupon, resources.get("plan_b_base_product")):
        raise RuntimeError(
            "Saved launch_coupon does not match either the current or supported legacy offer."
        )

    promotion_id = resources.get("launch_promotion_code")
    if promotion_id is not None:
        promotion = client.v1.promotion_codes.retrieve(promotion_id)
        promotion_coupon = _reference_id(
            object_value(object_value(promotion, "promotion", {}), "coupon"), "Coupon"
        )
        if promotion_coupon != coupon_id:
            raise RuntimeError("Saved LAUNCH20 Promotion Code points to an unexpected Coupon.")
        if object_value(promotion, "active", True) is True:
            update = getattr(client.v1.promotion_codes, "update", None)
            if update is None:
                raise RuntimeError("Stripe client cannot deactivate the legacy Promotion Code.")
            deactivated = update(promotion_id, {"active": False})
            if object_value(deactivated, "active") is not False:
                raise RuntimeError("Stripe did not deactivate the legacy Promotion Code.")
    store.remove_resources("launch_promotion_code", "launch_coupon")
    resources.pop("launch_promotion_code", None)
    resources.pop("launch_coupon", None)


def _bind_account(client: Any, store: CatalogStore) -> None:
    raw_request = getattr(client, "raw_request", None)
    if raw_request is None:
        return
    response = raw_request("get", "/v1/account")
    body = json.loads(response.body)
    account_id = body.get("id")
    if not isinstance(account_id, str):
        raise RuntimeError("The Stripe key did not resolve to an account.")
    store.save_account_id(account_id)


def _discover_existing(client: Any, store: CatalogStore, resources: dict[str, str]) -> None:
    """Recover a catalog when local state is missing or only partially written.

    Prices and Promotion Codes provide the strongest deterministic anchors:
    their configured keys point back to the exact Product, Meter, and Coupon.
    Product metadata then recovers a setup interrupted before its first Price.
    Discovery completes before any new create request is made, so an API/listing
    failure cannot leave another half-created catalog behind.
    """

    discovered: dict[str, str] = {}
    price_specs = (
        ("plan_a_price", product.PLAN_A_LOOKUP_KEY, "plan_a_product"),
        ("plan_b_base_price", product.PLAN_B_BASE_LOOKUP_KEY, "plan_b_base_product"),
        (
            "plan_b_overage_price",
            product.PLAN_B_OVERAGE_LOOKUP_KEY,
            "plan_b_overage_product",
        ),
    )
    for price_name, lookup_key, product_name in price_specs:
        if price_name in resources:
            continue
        price = _find_unique(
            f"active Price with lookup key {lookup_key}",
            (
                candidate
                for candidate in _list_objects(
                    client.v1.prices,
                    {"active": True, "lookup_keys": [lookup_key], "limit": 100},
                )
                if object_value(candidate, "lookup_key") == lookup_key
                and object_value(candidate, "active", True) is True
            ),
        )
        if price is None:
            continue
        discovered[price_name] = _test_object_id(price, "Price")
        product_id = _reference_id(object_value(price, "product"), "Product")
        _merge_discovery(resources, discovered, product_name, product_id)
        if price_name == "plan_b_overage_price":
            recurring = object_value(price, "recurring", {})
            meter_id = _reference_id(object_value(recurring, "meter"), "Billing Meter")
            _merge_discovery(resources, discovered, "plan_b_meter", meter_id)

    if "launch_promotion_code" not in resources:
        promotion = _find_unique(
            f"active Promotion Code {product.PROMOTION_CODE}",
            (
                candidate
                for candidate in _list_objects(
                    client.v1.promotion_codes,
                    {"active": True, "code": product.PROMOTION_CODE, "limit": 100},
                )
                if object_value(candidate, "active", True) is True
                and object_value(candidate, "code") == product.PROMOTION_CODE
            ),
        )
        if promotion is not None:
            discovered["launch_promotion_code"] = _test_object_id(promotion, "Promotion Code")
            promotion_details = object_value(promotion, "promotion", {})
            coupon_id = _reference_id(object_value(promotion_details, "coupon"), "Coupon")
            _merge_discovery(resources, discovered, "launch_coupon", coupon_id)

    product_specs = (
        ("plan_a_product", product.PLAN_A_NAME, product.PLAN_A_CODE, None),
        (
            "plan_b_base_product",
            product.PLAN_B_BASE_NAME,
            product.PLAN_B_CODE,
            product.PLAN_B_BASE_COMPONENT,
        ),
        (
            "plan_b_overage_product",
            product.PLAN_B_OVERAGE_NAME,
            product.PLAN_B_CODE,
            product.PLAN_B_OVERAGE_COMPONENT,
        ),
    )
    missing_products = [spec for spec in product_specs if spec[0] not in resources | discovered]
    if missing_products:
        candidates = _list_objects(client.v1.products, {"active": True, "limit": 100})
        for resource_name, expected_name, plan, component in missing_products:
            match = _find_unique(
                f"demo Product {expected_name}",
                (
                    candidate
                    for candidate in candidates
                    if _matches_product(candidate, expected_name, plan, component)
                ),
            )
            if match is not None:
                discovered[resource_name] = _test_object_id(match, "Product")

    if "plan_b_meter" not in resources | discovered:
        meter = _find_unique(
            f"active Billing Meter with event name {product.METER_EVENT_NAME}",
            (
                candidate
                for candidate in _list_objects(
                    client.v1.billing.meters, {"status": "active", "limit": 100}
                )
                if object_value(candidate, "status") == "active"
                and object_value(candidate, "event_name") == product.METER_EVENT_NAME
            ),
        )
        if meter is not None:
            discovered["plan_b_meter"] = _test_object_id(meter, "Billing Meter")

    if "launch_coupon" not in resources | discovered:
        all_resources = resources | discovered
        product_resources = {
            "plan_a": all_resources.get("plan_a_product", ""),
            "plan_b_base": all_resources.get("plan_b_base_product", ""),
            "plan_b_overage": all_resources.get("plan_b_overage_product", ""),
        }
        coupon_products = [product_resources[name] for name in product.COUPON_APPLIES_TO]
        coupon = None
        if coupon_products and all(coupon_products):
            coupon = _find_unique(
                "demo launch Coupon",
                (
                    candidate
                    for candidate in _list_objects(
                        client.v1.coupons,
                        {"limit": 100, "expand": ["data.applies_to"]},
                    )
                    if _matches_coupon(candidate, coupon_products)
                ),
            )
        if coupon is not None:
            discovered["launch_coupon"] = _test_object_id(coupon, "Coupon")

    for name, resource_id in discovered.items():
        if name not in resources:
            store.save_resource(name, resource_id)
            resources[name] = resource_id


def _list_objects(service: Any, params: dict[str, Any]) -> list[object]:
    list_method = getattr(service, "list", None)
    if list_method is None:
        return []
    page = list_method(params)
    auto_paging_iter = getattr(page, "auto_paging_iter", None)
    if auto_paging_iter is not None:
        return list(auto_paging_iter())
    data = object_value(page, "data", [])
    if not isinstance(data, list):
        raise RuntimeError("Stripe returned an invalid catalog listing.")
    return data


def _find_unique(label: str, candidates: Any) -> object | None:
    matches = list(candidates)
    if not matches:
        return None
    if len(matches) > 1:
        raise RuntimeError(
            f"Found multiple {label} objects in this Stripe test account. "
            "Restore the matching .local/catalog.json or archive the duplicate objects "
            "before running the demo again."
        )
    return matches[0]


def _test_object_id(value: object, label: str) -> str:
    require_test_object(value, label)
    return object_id(value, label)


def _reference_id(value: object, label: str) -> str:
    if isinstance(value, str):
        if not value:
            raise RuntimeError(f"Stripe returned an invalid {label} reference.")
        return value
    return object_id(value, label)


def _merge_discovery(
    resources: dict[str, str], discovered: dict[str, str], name: str, value: str
) -> None:
    existing = resources.get(name) or discovered.get(name)
    if existing is not None and existing != value:
        raise RuntimeError(
            f"Stripe catalog discovery found conflicting {name} objects. "
            "Restore the matching .local/catalog.json or archive the duplicate objects."
        )
    discovered[name] = value


def _matches_product(candidate: object, name: str, plan: str, component: str | None) -> bool:
    metadata = object_value(candidate, "metadata", {})
    expected_sample = product.CATALOG_METADATA["sample"]
    return (
        object_value(candidate, "active", True) is True
        and object_value(candidate, "name") == name
        and object_value(metadata, "sample") == expected_sample
        and object_value(metadata, product.PRODUCT_PLAN_METADATA_KEY) == plan
        and object_value(metadata, product.PRODUCT_COMPONENT_METADATA_KEY) == component
    )


def _matches_coupon(candidate: object, coupon_products: list[str]) -> bool:
    metadata = object_value(candidate, "metadata", {})
    applies_to = object_value(candidate, "applies_to", {})
    products = object_value(applies_to, "products", [])
    return (
        object_value(candidate, "valid", True) is True
        and object_value(candidate, "name") == product.COUPON_NAME
        and object_value(candidate, "percent_off") == float(product.COUPON_PERCENT_OFF)
        and object_value(candidate, "duration") == product.COUPON_DURATION
        and object_value(metadata, "sample") == product.CATALOG_METADATA["sample"]
        and set(products) == set(coupon_products)
    )


def _matches_legacy_coupon(candidate: object, plan_b_base_product: str | None) -> bool:
    metadata = object_value(candidate, "metadata", {})
    products = object_value(object_value(candidate, "applies_to", {}), "products", [])
    return (
        plan_b_base_product is not None
        and object_value(candidate, "valid", True) is True
        and object_value(candidate, "name") == _LEGACY_COUPON_NAME
        and object_value(candidate, "percent_off") == float(product.COUPON_PERCENT_OFF)
        and object_value(candidate, "duration") == product.COUPON_DURATION
        and object_value(metadata, "sample") == product.CATALOG_METADATA["sample"]
        and products == [plan_b_base_product]
    )


def _reconcile_existing(client: Any, catalog: Catalog, names: set[str]) -> None:
    specs = {
        "plan_a_product": (
            client.v1.products,
            catalog.plan_a_product,
            {"active": True, "name": product.PLAN_A_NAME},
        ),
        "plan_b_base_product": (
            client.v1.products,
            catalog.plan_b_base_product,
            {"active": True, "name": product.PLAN_B_BASE_NAME},
        ),
        "plan_b_overage_product": (
            client.v1.products,
            catalog.plan_b_overage_product,
            {"active": True, "name": product.PLAN_B_OVERAGE_NAME},
        ),
        "plan_a_price": (
            client.v1.prices,
            catalog.plan_a_price,
            {
                "active": True,
                "currency": product.CURRENCY,
                "unit_amount": product.PLAN_A_MONTHLY_CENTS,
                "tax_behavior": product.TAX_BEHAVIOR,
                "product": catalog.plan_a_product,
                "recurring.interval": product.BILLING_INTERVAL,
                "recurring.usage_type": "licensed",
            },
        ),
        "plan_b_base_price": (
            client.v1.prices,
            catalog.plan_b_base_price,
            {
                "active": True,
                "currency": product.CURRENCY,
                "unit_amount": product.PLAN_B_BASE_MONTHLY_CENTS,
                "tax_behavior": product.TAX_BEHAVIOR,
                "product": catalog.plan_b_base_product,
                "recurring.interval": product.BILLING_INTERVAL,
                "recurring.usage_type": "licensed",
            },
        ),
        "plan_b_overage_price": (
            client.v1.prices,
            catalog.plan_b_overage_price,
            {
                "active": True,
                "currency": product.CURRENCY,
                "unit_amount": product.PLAN_B_OVERAGE_PACKAGE_CENTS,
                "tax_behavior": product.TAX_BEHAVIOR,
                "product": catalog.plan_b_overage_product,
                "recurring.interval": product.BILLING_INTERVAL,
                "recurring.usage_type": "metered",
                "recurring.meter": catalog.plan_b_meter,
                "transform_quantity.divide_by": product.PLAN_B_OVERAGE_PACKAGE_GB,
                "transform_quantity.round": "up",
            },
        ),
        "plan_b_meter": (
            client.v1.billing.meters,
            catalog.plan_b_meter,
            {
                "status": "active",
                "event_name": product.METER_EVENT_NAME,
                "default_aggregation.formula": product.METER_AGGREGATION,
                "customer_mapping.event_payload_key": product.METER_CUSTOMER_PAYLOAD_KEY,
                "value_settings.event_payload_key": product.METER_VALUE_PAYLOAD_KEY,
            },
        ),
        "launch_coupon": (
            client.v1.coupons,
            catalog.launch_coupon,
            {
                "valid": True,
                "percent_off": float(product.COUPON_PERCENT_OFF),
                "duration": product.COUPON_DURATION,
            },
        ),
        "launch_promotion_code": (
            client.v1.promotion_codes,
            catalog.launch_promotion_code,
            {
                "active": True,
                "code": product.PROMOTION_CODE,
                "promotion.type": "coupon",
                "promotion.coupon": catalog.launch_coupon,
            },
        ),
    }
    for name in names:
        service, resource_id, expected = specs[name]
        try:
            resource = service.retrieve(resource_id)
            require_test_object(resource, name)
            for path, value in expected.items():
                if _nested_value(resource, path) != value:
                    raise RuntimeError(f"field {path} does not match")
        except Exception as exc:
            raise RuntimeError(
                f"Saved {name} cannot be reconciled with the intended test catalog. "
                "Use the matching Stripe test account and restore .local/catalog.json, or move "
                ".local aside before intentionally creating a separate catalog."
            ) from exc
    coupon = client.v1.coupons.retrieve(
        catalog.launch_coupon,
        {"expand": ["applies_to"]},
    )
    products = object_value(object_value(coupon, "applies_to", {}), "products", [])
    expected_coupon_products = _coupon_products(
        catalog.plan_a_product,
        catalog.plan_b_base_product,
        catalog.plan_b_overage_product,
    )
    if set(products) != set(expected_coupon_products):
        raise RuntimeError("Saved launch_coupon is not scoped to both eligible plan Products.")


def _nested_value(resource: object, path: str) -> Any:
    value = resource
    for part in path.split("."):
        value = object_value(value, part)
    return value
