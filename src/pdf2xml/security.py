"""Bearer credentials map to stable tenant names; credentials never enter job records."""
from __future__ import annotations

import hmac
import json
import os

from fastapi import HTTPException, Request


def credentials() -> dict[str, str]:
    raw = os.environ.get("PDF2XML_API_KEYS", "{}")
    try:
        keys = json.loads(raw)
        if not isinstance(keys, dict) or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or len(v) < 32
            for k, v in keys.items()
        ) or len(set(keys.values())) != len(keys):
            raise ValueError
    except (ValueError, TypeError):
        raise RuntimeError("PDF2XML_API_KEYS must map unique tenant names to unique tokens of at least 32 characters") from None
    if not keys and os.environ.get("PDF2XML_ALLOW_ANONYMOUS") != "1":
        raise RuntimeError("Configure PDF2XML_API_KEYS, or explicitly enable local development with PDF2XML_ALLOW_ANONYMOUS=1")
    return keys


def authenticate(request: Request) -> str:
    keys = credentials()
    if not keys:
        return "local"
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        for owner, expected in keys.items():
            if hmac.compare_digest(token.encode(), expected.encode()):
                return owner
    raise HTTPException(401, "Valid bearer credentials required", headers={"WWW-Authenticate": "Bearer"})
