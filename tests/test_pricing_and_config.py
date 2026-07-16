from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import dotenv_values

from streaming_billing import product_config
from streaming_billing.config import (
    ConfigurationError,
    Settings,
    local_base_url,
    require_loopback_host,
)
from streaming_billing.pricing import excess_gb, overage_blocks, plan_b_total_cents


@pytest.mark.parametrize(
    ("total", "excess", "blocks", "cents"),
    [
        ("0", 0, 0, 1_099),
        ("100", 0, 0, 1_099),
        ("100.0001", 1, 1, 1_199),
        ("101", 1, 1, 1_199),
        ("110", 10, 1, 1_199),
        ("110.0001", 11, 2, 1_299),
        ("120", 20, 2, 1_299),
    ],
)
def test_started_block_boundaries(total: str, excess: int, blocks: int, cents: int) -> None:
    usage = Decimal(total)
    assert excess_gb(usage) == excess
    assert overage_blocks(usage) == blocks
    assert plan_b_total_cents(usage) == cents


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "-Infinity"])
def test_invalid_usage_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        excess_gb(Decimal(value))


def test_settings_accept_server_side_test_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_" + "A" * 24)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_" + "B" * 24)
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))

    settings = Settings.from_env(require_webhook=True, load_env_file=False)

    assert settings.success_url == (
        "http://127.0.0.1:8000/success?session_id={CHECKOUT_SESSION_ID}"
    )
    assert settings.state_dir == tmp_path


@pytest.mark.parametrize(
    "key",
    [
        "sk_live_" + "A" * 24,
        "rk_live_" + "A" * 24,
        "pk_test_" + "A" * 24,
        "rk_test_" + "A" * 24,
        "sk_test_replace_me",
        "",
    ],
)
def test_settings_reject_non_server_test_keys(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", key)
    with pytest.raises(ConfigurationError, match="server-side"):
        Settings.from_env(load_env_file=False)


def test_base_url_cannot_supply_a_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_" + "A" * 24)
    monkeypatch.setenv("APP_BASE_URL", "https://example.com/attacker-path?x=1")
    with pytest.raises(ConfigurationError, match="query"):
        Settings.from_env(load_env_file=False)


def test_literal_env_example_does_not_require_webhook_for_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = dotenv_values(Path(__file__).parents[1] / ".env.example")
    for key, value in example.items():
        monkeypatch.setenv(key, value or "")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_" + "A" * 24)
    assert Settings.from_env(load_env_file=False).webhook_secret is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_non_loopback_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ConfigurationError, match="loopback"):
        require_loopback_host(host)


def test_non_loopback_base_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_" + "A" * 24)
    monkeypatch.setenv("APP_BASE_URL", "http://0.0.0.0:8000")
    with pytest.raises(ConfigurationError, match="loopback"):
        Settings.from_env(load_env_file=False)


def test_customer_product_config_contains_all_commercial_choices() -> None:
    assert product_config.PLAN_A_MONTHLY_CENTS == 2_499
    assert product_config.PLAN_B_BASE_MONTHLY_CENTS == 1_099
    assert product_config.PLAN_B_INCLUDED_GB == Decimal("100")
    assert product_config.PLAN_B_OVERAGE_PACKAGE_GB == 10
    assert product_config.PLAN_B_OVERAGE_PACKAGE_CENTS == 100
    assert product_config.METER_AGGREGATION == "last"
    assert product_config.TAX_BEHAVIOR == "exclusive"
    assert product_config.COUPON_PERCENT_OFF == 20
    assert product_config.COUPON_APPLIES_TO == ("plan_a", "plan_b_base")
    assert product_config.PROMOTION_CODE == "LAUNCH20"
    assert product_config.PLAN_A_LOOKUP_KEY
    assert product_config.PLAN_B_BASE_LOOKUP_KEY
    assert product_config.PLAN_B_OVERAGE_LOOKUP_KEY
    assert product_config.CATALOG_METADATA["sample"]
    assert product_config.PRODUCT_PLAN_METADATA_KEY == "plan"
    assert product_config.PLAN_METADATA_KEY == "streaming_plan"


def test_local_base_url_uses_server_port_and_supports_ipv6() -> None:
    assert local_base_url("127.0.0.1", 8765) == "http://127.0.0.1:8765"
    assert local_base_url("::1", 8765) == "http://[::1]:8765"
