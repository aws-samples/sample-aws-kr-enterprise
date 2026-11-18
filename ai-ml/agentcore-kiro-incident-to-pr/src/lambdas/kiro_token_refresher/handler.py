"""Kiro web login token refresher (kiroAuthMode=web).

Kiro web login access tokens expire after about an hour. Every 15 minutes this function refreshes the token with
the stored refresh token (AWS IAM Identity Center / Builder ID OIDC) when less than 30 minutes remain, and writes
only the access token to the secret the AgentCore Runtime reads. The refresh token never leaves this function.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets = boto3.client("secretsmanager")
LOGIN_SECRET = os.environ["LOGIN_SECRET_ID"]
TOKEN_SECRET = os.environ["TOKEN_SECRET_ID"]
REFRESH_BEFORE = timedelta(minutes=int(os.environ.get("REFRESH_BEFORE_MINUTES", "30")))


def _read(secret_id):
    return json.loads(secrets.get_secret_value(SecretId=secret_id)["SecretString"])


def handler(event, context):
    login = _read(LOGIN_SECRET)
    try:
        token = _read(TOKEN_SECRET)
        expires = datetime.fromisoformat(token["expiresAt"].replace("Z", "+00:00"))
    except Exception:
        token, expires = {}, datetime.min.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if expires - now > REFRESH_BEFORE and not (event or {}).get("force"):
        return {"status": "FRESH", "expiresAt": token["expiresAt"]}
    oidc = boto3.client("sso-oidc", region_name=login["region"])
    reply = oidc.create_token(clientId=login["clientId"], clientSecret=login["clientSecret"],
                              grantType="refresh_token", refreshToken=login["refreshToken"])
    if reply.get("refreshToken") and reply["refreshToken"] != login["refreshToken"]:
        login["refreshToken"] = reply["refreshToken"]
        secrets.put_secret_value(SecretId=LOGIN_SECRET, SecretString=json.dumps(login))
    expires_at = (now + timedelta(seconds=reply["expiresIn"])).isoformat().replace("+00:00", "Z")
    record = {"accessToken": reply["accessToken"], "expiresAt": expires_at}
    if login.get("profileArn"):
        record["profileArn"] = login["profileArn"]
    secrets.put_secret_value(SecretId=TOKEN_SECRET, SecretString=json.dumps(record))
    logger.info("Kiro web token refreshed; expires at %s", expires_at)
    return {"status": "REFRESHED", "expiresAt": expires_at}
