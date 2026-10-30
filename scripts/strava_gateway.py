"""Configuration for the independently hosted Strava gateway."""

import os
from typing import Dict, Tuple
from urllib.parse import urlsplit


def normalize_gateway_url(value: str) -> str:
    """Accept a service origin or its API prefix, without embedded credentials."""
    value = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Validate any explicit port.
    except ValueError:
        raise ValueError("STRAVA_GATEWAY_URL must be a valid gateway URL.") from None
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname
        or (parsed.scheme != "https" and not local_http)
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
        or parsed.path not in {"", "/v1/strava"}
        or any(char.isspace() for char in value)
        or "\\" in value
    ):
        raise ValueError(
            "STRAVA_GATEWAY_URL must be an HTTPS service origin (optionally ending "
            "in /v1/strava), without credentials, query, or fragment. "
            "HTTP is allowed only for localhost development."
        )
    return value.removesuffix("/v1/strava")


def gateway_settings(config: Dict) -> Tuple[str, str]:
    settings = config.get("strava_gateway") or {}
    url = os.environ.get("STRAVA_GATEWAY_URL") or settings.get("url", "")
    key = os.environ.get("STRAVA_GATEWAY_CLIENT_KEY") or settings.get("client_key", "")
    if not url or not str(key).strip():
        raise ValueError(
            "Strava gateway requires STRAVA_GATEWAY_URL and STRAVA_GATEWAY_CLIENT_KEY "
            "or strava_gateway.url and strava_gateway.client_key in config.local.yaml."
        )
    return normalize_gateway_url(url) + "/v1/strava", str(key).strip()


def uses_gateway(config: Dict) -> bool:
    return str(config.get("source", "")).strip().lower() == "strava-gateway"
