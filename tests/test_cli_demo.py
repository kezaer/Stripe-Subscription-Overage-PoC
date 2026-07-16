from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from streaming_billing import cli
from streaming_billing.config import ConfigurationError, Settings
from streaming_billing.state import Catalog


def _settings(tmp_path, *, base_url: str = "http://127.0.0.1:8000") -> Settings:
    return Settings(
        stripe_secret_key="sk_test_" + "A" * 24,
        webhook_secret=None,
        base_url=base_url,
        state_dir=tmp_path,
    )


def _catalog() -> Catalog:
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


def test_demo_prompts_for_missing_key_without_writing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    key = "sk_test_" + "K" * 24
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: key)

    settings = cli._demo_settings("http://127.0.0.1:8765")

    assert settings.stripe_secret_key == key
    assert settings.base_url == "http://127.0.0.1:8765"


def test_demo_provisions_catalog_and_starts_without_webhook_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _settings(tmp_path, base_url="http://127.0.0.1:8765")
    client = object()
    app = object()
    calls: list[str] = []
    monkeypatch.setattr(cli, "_demo_settings", lambda base_url: settings)
    monkeypatch.setattr(cli, "stripe_client", lambda configured: client)

    def provision(configured_client, store):
        assert configured_client is client
        calls.append("provision")
        return _catalog()

    monkeypatch.setattr(cli, "provision_catalog", provision)
    import streaming_billing.web as web

    def create_app(configured, configured_client):
        assert configured.webhook_secret is None
        assert configured.thin_webhook_secret is None
        assert configured_client is client
        calls.append("app")
        return app

    monkeypatch.setattr(web, "create_app", create_app)

    def run(configured_app, **kwargs):
        assert configured_app is app
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8765
        calls.append("run")

    monkeypatch.setattr(cli.uvicorn, "run", run)

    cli._demo("127.0.0.1", 8765, no_open=True, with_webhooks=False)

    assert calls == ["provision", "app", "run"]


def test_stripe_listener_uses_child_env_and_captures_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "sk_test_" + "A" * 24
    secret = "whsec_" + "B" * 24
    captured: dict[str, object] = {}

    class Process:
        def __init__(self):
            self.stdout = io.StringIO(f"Ready! Your webhook signing secret is {secret}\n")
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0

        def kill(self):
            self.terminated = True

    process = Process()

    def popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return process

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/stripe")
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    listener = cli.StripeCliListener(key, "http://127.0.0.1:8000")

    assert listener.start() == secret
    command = captured["command"]
    assert isinstance(command, list)
    assert key not in command
    assert command[command.index("--events") + 1] == ",".join(cli._SNAPSHOT_EVENTS)
    assert "--forward-to" in command
    assert "--thin-events" in command
    assert captured["env"]["STRIPE_API_KEY"] == key
    assert all(secret not in line for line in listener._startup_lines)

    listener.stop()
    assert process.terminated is True


def test_full_demo_supplies_listener_secret_and_stops_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _settings(tmp_path)
    client = object()
    secret = "whsec_" + "B" * 24
    lifecycle: list[str] = []
    monkeypatch.setattr(cli, "_demo_settings", lambda base_url: settings)
    monkeypatch.setattr(cli, "stripe_client", lambda configured: client)
    monkeypatch.setattr(cli, "provision_catalog", lambda configured, store: _catalog())

    class Listener:
        def __init__(self, key, base_url):
            assert key == settings.stripe_secret_key
            lifecycle.append("listener-created")

        def start(self):
            lifecycle.append("listener-started")
            return secret

        def stop(self):
            lifecycle.append("listener-stopped")

    monkeypatch.setattr(cli, "StripeCliListener", Listener)
    import streaming_billing.web as web

    def create_app(configured, configured_client):
        assert configured.webhook_secret == secret
        assert configured.thin_webhook_secret == secret
        lifecycle.append("app-created")
        return object()

    monkeypatch.setattr(web, "create_app", create_app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: lifecycle.append("app-run"))

    cli._demo("127.0.0.1", 8000, no_open=True, with_webhooks=True)

    assert lifecycle == [
        "listener-created",
        "listener-started",
        "app-created",
        "app-run",
        "listener-stopped",
    ]


def test_with_webhooks_reports_missing_stripe_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    listener = cli.StripeCliListener(
        "sk_test_" + "A" * 24,
        "http://127.0.0.1:8000",
    )
    with pytest.raises(ConfigurationError, match="requires the Stripe CLI"):
        listener.start()


def test_serve_port_overrides_stale_callback_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured: dict[str, object] = {}
    settings = _settings(tmp_path, base_url="http://127.0.0.1:8765")

    def from_env(**kwargs):
        captured.update(kwargs)
        return settings

    monkeypatch.setattr(cli.Settings, "from_env", from_env)
    import streaming_billing.web as web

    monkeypatch.setattr(web, "create_app", lambda configured: SimpleNamespace())
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    cli._serve("127.0.0.1", 8765, reload=False)

    assert captured["base_url"] == "http://127.0.0.1:8765"
    assert captured["require_webhook"] is False
