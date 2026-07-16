from __future__ import annotations

from types import SimpleNamespace

import pytest

from streaming_billing.catalog import provision_catalog
from streaming_billing.checkout import checkout_params, create_checkout_session
from streaming_billing.config import Settings
from streaming_billing.pricing import Plan
from streaming_billing.state import CATALOG_FIELDS, Catalog, CatalogStore


class RecordingService:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.calls: list[tuple[dict, dict | None]] = []
        self.list_calls: list[dict] = []
        self.retrieve_calls: list[tuple[str, dict | None]] = []
        self.objects: dict[str, SimpleNamespace] = {}

    def create(self, params: dict, options: dict | None = None):
        self.calls.append((params, options))
        result = SimpleNamespace(
            id=f"{self.prefix}test_{len(self.calls)}",
            livemode=False,
            active=True,
            status="active",
            valid=True,
            **{key: _namespace(value) for key, value in params.items()},
        )
        self.objects[result.id] = result
        return result

    def retrieve(self, resource_id: str, params: dict | None = None):
        self.retrieve_calls.append((resource_id, params))
        return self.objects[resource_id]

    def list(self, params: dict):
        self.list_calls.append(params)
        return SimpleNamespace(data=list(self.objects.values()))

    def update(self, resource_id: str, params: dict):
        resource = self.objects[resource_id]
        for key, value in params.items():
            setattr(resource, key, _namespace(value))
        return resource


class CheckoutService(RecordingService):
    def __init__(self) -> None:
        super().__init__("cs_test_")

    def create(self, params: dict, options: dict | None = None):
        result = super().create(params, options)
        result.url = "https://checkout.stripe.com/c/pay/test"
        return result


def fake_client():
    products = RecordingService("prod_")
    prices = RecordingService("price_")
    meters = RecordingService("mtr_")
    coupons = RecordingService("coupon_")
    promotions = RecordingService("promo_")
    checkout = CheckoutService()
    client = SimpleNamespace(
        v1=SimpleNamespace(
            products=products,
            prices=prices,
            billing=SimpleNamespace(meters=meters),
            coupons=coupons,
            promotion_codes=promotions,
            checkout=SimpleNamespace(sessions=checkout),
        )
    )
    return client, products, prices, meters, coupons, promotions, checkout


def _namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


def sample_catalog() -> Catalog:
    return Catalog(
        plan_a_product="prod_a",
        plan_a_price="price_a",
        plan_b_base_product="prod_b_base",
        plan_b_base_price="price_b_base",
        plan_b_overage_product="prod_b_overage",
        plan_b_meter="mtr_b",
        plan_b_overage_price="price_b_overage",
        launch_coupon="coupon_launch",
        launch_promotion_code="promo_launch",
    )


def sample_settings(tmp_path) -> Settings:
    return Settings(
        stripe_secret_key="sk_test_" + "A" * 24,
        webhook_secret="whsec_" + "B" * 24,
        base_url="http://127.0.0.1:8000",
        state_dir=tmp_path,
    )


def test_catalog_provisioning_saves_and_reuses_all_resources(tmp_path) -> None:
    client, products, prices, meters, coupons, promotions, _ = fake_client()
    store = CatalogStore(tmp_path)

    first = provision_catalog(client, store)
    second = provision_catalog(client, store)

    assert first == second
    assert set(store.resources()) == set(CATALOG_FIELDS)
    assert len(products.calls) == 3
    assert len(prices.calls) == 3
    assert len(meters.calls) == 1
    assert len(coupons.calls) == 1
    assert len(promotions.calls) == 1

    metered_price = prices.calls[2][0]
    assert metered_price["recurring"] == {
        "interval": "month",
        "usage_type": "metered",
        "meter": first.plan_b_meter,
    }
    assert metered_price["transform_quantity"] == {"divide_by": 10, "round": "up"}
    assert coupons.calls[0][0]["applies_to"] == {
        "products": [first.plan_a_product, first.plan_b_base_product]
    }
    assert coupons.calls[0][0]["duration"] == "once"
    assert promotions.calls[0][0]["promotion"] == {
        "type": "coupon",
        "coupon": first.launch_coupon,
    }
    assert all(
        call[1] and call[1]["idempotency_key"]
        for service in (products, prices, meters, coupons, promotions)
        for call in service.calls
    )


def test_legacy_plan_b_only_launch_offer_migrates_without_duplicate_active_code(
    tmp_path,
) -> None:
    client, _, _, _, coupons, promotions, _ = fake_client()
    store = CatalogStore(tmp_path)
    legacy = provision_catalog(client, store)
    legacy_coupon = coupons.objects[legacy.launch_coupon]
    legacy_coupon.name = "20% off the first Plan B base invoice"
    legacy_coupon.applies_to.products = [legacy.plan_b_base_product]

    migrated = provision_catalog(client, store)

    assert migrated.launch_coupon != legacy.launch_coupon
    assert migrated.launch_promotion_code != legacy.launch_promotion_code
    assert promotions.objects[legacy.launch_promotion_code].active is False
    assert coupons.objects[migrated.launch_coupon].applies_to.products == [
        legacy.plan_a_product,
        legacy.plan_b_base_product,
    ]
    active_launch_codes = [
        item
        for item in promotions.objects.values()
        if item.code == "LAUNCH20" and item.active is True
    ]
    assert [item.id for item in active_launch_codes] == [migrated.launch_promotion_code]


def test_catalog_reconciliation_rejects_changed_remote_price(tmp_path) -> None:
    client, _, prices, *_ = fake_client()
    store = CatalogStore(tmp_path)
    catalog = provision_catalog(client, store)
    prices.objects[catalog.plan_a_price].unit_amount = 999
    with pytest.raises(RuntimeError, match="cannot be reconciled"):
        provision_catalog(client, store)


def test_catalog_reconciliation_rejects_wrong_price_product(tmp_path) -> None:
    client, _, prices, *_ = fake_client()
    store = CatalogStore(tmp_path)
    catalog = provision_catalog(client, store)
    prices.objects[catalog.plan_b_overage_price].product = "prod_wrong"
    with pytest.raises(RuntimeError, match="cannot be reconciled"):
        provision_catalog(client, store)


def test_catalog_reconciliation_rejects_wrong_promotion_coupon(tmp_path) -> None:
    client, *_, promotions, _ = fake_client()
    store = CatalogStore(tmp_path)
    catalog = provision_catalog(client, store)
    promotions.objects[catalog.launch_promotion_code].promotion.coupon = "coupon_wrong"
    with pytest.raises(RuntimeError, match="cannot be reconciled"):
        provision_catalog(client, store)


def test_new_catalog_state_uses_a_distinct_idempotency_namespace(tmp_path) -> None:
    first_client, first_products, *_ = fake_client()
    second_client, second_products, *_ = fake_client()
    provision_catalog(first_client, CatalogStore(tmp_path / "one"))
    provision_catalog(second_client, CatalogStore(tmp_path / "two"))
    first_key = first_products.calls[0][1]["idempotency_key"]
    second_key = second_products.calls[0][1]["idempotency_key"]
    assert first_key != second_key


def test_missing_local_state_recovers_remote_catalog_without_duplicates(tmp_path) -> None:
    client, products, prices, meters, coupons, promotions, _ = fake_client()
    original = provision_catalog(client, CatalogStore(tmp_path / "first"))
    create_counts = tuple(
        len(service.calls) for service in (products, prices, meters, coupons, promotions)
    )

    recovered_store = CatalogStore(tmp_path / "recovered")
    recovered = provision_catalog(client, recovered_store)

    assert recovered == original
    assert recovered_store.is_complete()
    assert (
        tuple(len(service.calls) for service in (products, prices, meters, coupons, promotions))
        == create_counts
    )
    assert prices.list_calls
    assert promotions.list_calls
    assert (original.launch_coupon, {"expand": ["applies_to"]}) in coupons.retrieve_calls


def test_ambiguous_remote_lookup_stops_before_creating_more_objects(tmp_path) -> None:
    client, products, prices, meters, coupons, promotions, _ = fake_client()
    original_store = CatalogStore(tmp_path / "first")
    catalog = provision_catalog(client, original_store)
    original_price = prices.objects[catalog.plan_a_price]
    prices.create(
        {
            "product": catalog.plan_a_product,
            "currency": original_price.currency,
            "unit_amount": original_price.unit_amount,
            "recurring": {"interval": "month", "usage_type": "licensed"},
            "tax_behavior": "exclusive",
            "lookup_key": original_price.lookup_key,
        }
    )
    create_counts = tuple(
        len(service.calls) for service in (products, prices, meters, coupons, promotions)
    )

    with pytest.raises(RuntimeError, match="multiple active Price"):
        provision_catalog(client, CatalogStore(tmp_path / "ambiguous"))

    assert (
        tuple(len(service.calls) for service in (products, prices, meters, coupons, promotions))
        == create_counts
    )


def test_checkout_payload_uses_hosted_subscription_contract(tmp_path) -> None:
    settings = sample_settings(tmp_path)
    catalog = sample_catalog()

    plan_a = checkout_params(catalog, Plan.A, settings)
    plan_b = checkout_params(catalog, Plan.B, settings)

    assert plan_a["line_items"] == [{"price": "price_a", "quantity": 1}]
    assert plan_b["line_items"] == [
        {"price": "price_b_base", "quantity": 1},
        {"price": "price_b_overage"},
    ]
    assert "quantity" not in plan_b["line_items"][1]
    assert plan_b["mode"] == "subscription"
    assert plan_b["allow_promotion_codes"] is True
    assert plan_b["subscription_data"]["billing_mode"] == {"type": "flexible"}
    assert plan_a["metadata"] == {"streaming_plan": "a"}
    assert plan_b["metadata"] == {"streaming_plan": "b"}
    assert plan_a["subscription_data"]["metadata"] == {"streaming_plan": "a"}
    assert plan_b["subscription_data"]["metadata"] == {"streaming_plan": "b"}
    assert plan_b["success_url"].endswith("session_id={CHECKOUT_SESSION_ID}")


def test_checkout_session_uses_caller_request_id(tmp_path) -> None:
    client, *_, checkout = fake_client()
    request_id = "7a68d606-2ee9-4f9e-a7fd-0f3625ef8824"

    url = create_checkout_session(
        client,
        sample_catalog(),
        Plan.B,
        sample_settings(tmp_path),
        request_id,
    )

    assert url.startswith("https://checkout.stripe.com/")
    assert checkout.calls[0][1] == {"idempotency_key": f"streaming-checkout-b-{request_id}"}


def test_checkout_rejects_non_stripe_redirect(tmp_path) -> None:
    client, *_, checkout = fake_client()

    def unsafe_create(params: dict, options: dict | None = None):
        return SimpleNamespace(id="cs_test_bad", livemode=False, url="https://example.com/phish")

    checkout.create = unsafe_create
    with pytest.raises(RuntimeError, match="invalid Checkout URL"):
        create_checkout_session(
            client,
            sample_catalog(),
            Plan.A,
            sample_settings(tmp_path),
            "7a68d606-2ee9-4f9e-a7fd-0f3625ef8824",
        )
