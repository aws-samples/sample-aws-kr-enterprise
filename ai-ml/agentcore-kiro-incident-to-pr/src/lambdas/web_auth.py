"""Shared web password for the demo Web UI, REST API and WebSocket API.

The Secrets Manager secret (created by the user before deployment) holds {"password", "signing_key"}. Logging in with
the password returns a token "<expiry>.<signature>" signed with signing_key, so a token reveals nothing that helps to
guess the password. The signature also covers a digest of the password: changing either value invalidates every
token within SECRET_CACHE_SECONDS.
"""
import hashlib
import hmac
import json
import os
import time

import boto3

SECRET_ID = os.environ.get("WEB_PASSWORD_SECRET_ID", "incident-to-pr/web-password")
SECRET_CACHE_SECONDS = 60
TOKEN_SECONDS = 12 * 3600
MIN_PASSWORD_LENGTH = 16
MIN_SIGNING_KEY_LENGTH = 32

_cache = {"value": None, "read_at": 0.0}


class NotConfigured(RuntimeError):
    """The web password secret is missing or too weak; every request is refused."""


def _credentials():
    if _cache["value"] is None or time.time() - _cache["read_at"] > SECRET_CACHE_SECONDS:
        try:
            record = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)["SecretString"])
            password, key = record["password"], record["signing_key"]
        except Exception as e:
            raise NotConfigured(f"cannot read the web password secret {SECRET_ID}") from e
        if not isinstance(password, str) or not isinstance(key, str) or (
                len(password) < MIN_PASSWORD_LENGTH or len(key) < MIN_SIGNING_KEY_LENGTH):
            raise NotConfigured(f"{SECRET_ID} needs a password of at least {MIN_PASSWORD_LENGTH} characters "
                                f"and a signing_key of at least {MIN_SIGNING_KEY_LENGTH}")
        _cache.update(value=(password, key), read_at=time.time())
    return _cache["value"]


def _signature(password, key, expiry):
    message = f"incident-to-pr-web.v1.{expiry}.{hashlib.sha256(password.encode()).hexdigest()}"
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def login(password):
    """A new token for the right password, otherwise None."""
    expected, key = _credentials()
    if not isinstance(password, str) or not hmac.compare_digest(password.encode(), expected.encode()):
        return None
    expiry = int(time.time()) + TOKEN_SECONDS
    return {"token": f"{expiry}.{_signature(expected, key, expiry)}", "expires_at": expiry}


def verify(token):
    if not isinstance(token, str) or len(token) > 128:
        return False
    expiry, _, signature = token.partition(".")
    if not expiry.isdigit() or int(expiry) <= time.time():
        return False
    password, key = _credentials()
    return hmac.compare_digest(signature, _signature(password, key, int(expiry)))


def bearer_token(headers):
    for name, value in (headers or {}).items():
        if name.lower() == "authorization" and isinstance(value, str) and value.startswith("Bearer "):
            return value[len("Bearer "):]
    return None
