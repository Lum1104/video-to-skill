"""Shared URL credential detection for generated, shareable artifacts."""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import parse_qsl, unquote_plus, urlsplit

MAX_URL_PARAMETER_FIELDS = 256

_SENSITIVE_URL_PARAMETER_NAMES = {
    "accesskey",
    "accesskeyid",
    "accesstoken",
    "apikey",
    "auth",
    "authtoken",
    "authorization",
    "awsaccesskeyid",
    "bearertoken",
    "clientsecret",
    "credential",
    "credentials",
    "expires",
    "expiration",
    "expiry",
    "googleaccessid",
    "idtoken",
    "jwt",
    "key",
    "keypairid",
    "oauthtoken",
    "oauthtokensecret",
    "password",
    "passwd",
    "policy",
    "privatekey",
    "refreshtoken",
    "secret",
    "securitytoken",
    "session",
    "sessionid",
    "sessiontoken",
    "sig",
    "signature",
    "token",
    "xamzcredential",
    "xamzdate",
    "xamzexpires",
    "xamzsecuritytoken",
    "xamzsignature",
    "xgoogcredential",
    "xgoogdate",
    "xgoogexpires",
    "xgoogsignature",
}
_SENSITIVE_URL_PARAMETER_SUFFIXES = (
    "accesskey",
    "accesskeyid",
    "accesstoken",
    "apikey",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "credential",
    "expires",
    "expiration",
    "idtoken",
    "jwt",
    "oauthtoken",
    "oauthtokensecret",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "securitytoken",
    "sessiontoken",
    "signature",
)


class UrlParameterLimitError(ValueError):
    """Raised when a URL has too many fields for bounded inspection."""


def is_sensitive_url_parameter_name(name: str) -> bool:
    """Return whether a decoded URL parameter name denotes credential material."""

    decoded = name
    for _ in range(2):
        next_value = unquote_plus(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    normalized = re.sub(r"[^a-z0-9]", "", decoded.casefold())
    return normalized in _SENSITIVE_URL_PARAMETER_NAMES or normalized.endswith(
        _SENSITIVE_URL_PARAMETER_SUFFIXES
    )


def _component_variants(component: str) -> Iterator[str]:
    """Yield bounded raw and decoded interpretations used by common URL consumers."""

    current = component
    seen: set[str] = set()
    for _ in range(3):
        candidate = current.replace(";", "&").replace("?", "&")
        if candidate not in seen:
            seen.add(candidate)
            yield candidate
        decoded = unquote_plus(current)
        if decoded == current:
            break
        current = decoded


def has_sensitive_url_parameters(
    value: str,
    *,
    max_fields: int = MAX_URL_PARAMETER_FIELDS,
) -> bool:
    """Inspect query and fragment fields using conservative web-compatible separators."""

    parsed = urlsplit(value)
    remaining = max_fields
    for component in (parsed.query, parsed.fragment):
        if not component:
            continue
        for variant_index, conservative_component in enumerate(_component_variants(component)):
            try:
                fields = parse_qsl(
                    conservative_component,
                    keep_blank_values=True,
                    max_num_fields=remaining if variant_index == 0 else max_fields,
                )
            except ValueError as exc:
                raise UrlParameterLimitError(
                    "URL parameter count exceeds the bounded security limit"
                ) from exc
            if variant_index == 0:
                remaining -= len(fields)
                if remaining < 0:
                    raise UrlParameterLimitError(
                        "URL parameter count exceeds the bounded security limit"
                    )
            if any(is_sensitive_url_parameter_name(name) for name, _value in fields):
                return True
    return False
