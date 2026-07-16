from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

STRIPE_API_VERSION = "2026-06-24.dahlia"
_TEST_KEY_PATTERN = re.compile(r"sk_test_[A-Za-z0-9]{16,}")
_WEBHOOK_SECRET_PATTERN = re.compile(r"whsec_[A-Za-z0-9]{16,}")


class ConfigurationError(ValueError):
    """Report missing or unsafe local configuration."""


@dataclass(frozen=True)
class Settings:
    stripe_secret_key: str
    webhook_secret: str | None
    base_url: str
    state_dir: Path
    thin_webhook_secret: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        require_webhook: bool = False,
        load_env_file: bool = True,
        base_url: str | None = None,
    ) -> Settings:
        if load_env_file:
            load_dotenv(override=False)
        secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        if not _TEST_KEY_PATTERN.fullmatch(secret_key):
            raise ConfigurationError("STRIPE_SECRET_KEY must be a server-side sk_test_ key.")

        webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip() or None
        if webhook_secret and not _WEBHOOK_SECRET_PATTERN.fullmatch(webhook_secret):
            raise ConfigurationError("STRIPE_WEBHOOK_SECRET must be a whsec_ signing secret.")
        if require_webhook and webhook_secret is None:
            raise ConfigurationError("Set STRIPE_WEBHOOK_SECRET before starting the web app.")
        thin_webhook_secret = (
            os.environ.get("STRIPE_THIN_WEBHOOK_SECRET", "").strip() or webhook_secret
        )
        if thin_webhook_secret and not _WEBHOOK_SECRET_PATTERN.fullmatch(thin_webhook_secret):
            raise ConfigurationError("STRIPE_THIN_WEBHOOK_SECRET must be a whsec_ signing secret.")

        validated_base_url = _validated_base_url(
            base_url or os.environ.get("APP_BASE_URL", "http://127.0.0.1:8000").strip()
        )
        state_dir = Path(os.environ.get("STATE_DIR", ".local")).expanduser()
        return cls(
            stripe_secret_key=secret_key,
            webhook_secret=webhook_secret,
            base_url=validated_base_url,
            state_dir=state_dir,
            thin_webhook_secret=thin_webhook_secret,
        )

    @property
    def success_url(self) -> str:
        return f"{self.base_url}/success?session_id={{CHECKOUT_SESSION_ID}}"

    @property
    def cancel_url(self) -> str:
        return f"{self.base_url}/?checkout=cancelled"


def _validated_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("APP_BASE_URL must be an http or https origin.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError("APP_BASE_URL cannot contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ConfigurationError("APP_BASE_URL cannot contain a path.")
    hostname = parsed.hostname
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"
    if not is_loopback:
        raise ConfigurationError("APP_BASE_URL must use localhost or a loopback IP address.")
    return value.rstrip("/")


def require_loopback_host(value: str) -> str:
    try:
        is_loopback = ipaddress.ip_address(value).is_loopback
    except ValueError:
        is_loopback = value == "localhost"
    if not is_loopback:
        raise ConfigurationError("--host must be localhost or a loopback IP address.")
    return value


def local_base_url(host: str, port: int) -> str:
    """Return the callback origin for the exact loopback server binding."""

    host = require_loopback_host(host)
    if not 1 <= port <= 65_535:
        raise ConfigurationError("--port must be between 1 and 65535.")
    try:
        is_ipv6 = ipaddress.ip_address(host).version == 6
    except ValueError:
        is_ipv6 = False
    rendered_host = f"[{host}]" if is_ipv6 else host
    return f"http://{rendered_host}:{port}"
