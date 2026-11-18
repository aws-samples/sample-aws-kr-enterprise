"""Kiro runtime for the incident agents: authentication, workspace, permission policy and one-shot agent turns."""
import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from kiro_agent_sdk import (KiroAgentOptions, KiroSDKClient, PermissionResultAllow, PermissionResultDeny,
                            ResultMessage)

logger = logging.getLogger("incident.kiro")

APP_DIR = Path(__file__).resolve().parent
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"))
STATE_DIR = Path(os.environ.get("INCIDENT_STATE_DIR", "/tmp/incident"))
WORKSPACE = STATE_DIR / "workspace"
KNOWLEDGE_DIR = STATE_DIR / "knowledge"
MODEL = os.environ.get("KIRO_MODEL", "claude-opus-5.5")
AUTH_MODE = os.environ.get("KIRO_AUTH_MODE", "apikey")
TURN_TIMEOUT = int(os.environ.get("KIRO_TURN_TIMEOUT", "900"))

# Subagents each role may delegate to, and the MCP tool servers reachable through that role's agent tree.
# Only the fixer reaches the change tool, and it can only open a pull request into the workload repository.
SUBAGENTS = {
    "collector": {"logs", "metrics", "infrastructure", "knowledge", "repository"},
    "chatbot": {"logs", "metrics", "infrastructure", "knowledge", "repository"},
}
SERVERS = {
    "collector": {"logs", "metrics", "infrastructure", "knowledge", "repository"},
    "chatbot": {"logs", "metrics", "infrastructure", "knowledge", "repository", "reports", "rca"},
    "fixer": {"repository", "changes"},
}


def _home():
    return STATE_DIR / "home"


def prepare():
    """Copy the Kiro workspace and link the Kiro runtime cache once per microVM.

    workspace/ is an ordinary Kiro project (.kiro/agents, steering, skills) that also runs in Kiro desktop.
    """
    if (WORKSPACE / ".kiro" / "agents" / "collector.md").exists():
        return
    data = _home() / ".local" / "share" / "kiro-cli"
    data.mkdir(parents=True, exist_ok=True)
    cache = Path(os.environ.get("KIRO_RUNTIME_CACHE", "/opt/kiro-prewarm"))
    for name in ("node", "node.sha256", "kas"):
        if (cache / name).exists() and not (data / name).exists():
            (data / name).symlink_to(cache / name)
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    shutil.copytree(APP_DIR / "workspace", WORKSPACE)


INTERNAL_TOKEN = os.urandom(24).hex()


def internal_url():
    return f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/internal"


# ---------------------------------------------------------------- authentication

_secret_cache = {"value": None, "read_at": 0.0}


def _secret():
    if _secret_cache["value"] is None or time.time() - _secret_cache["read_at"] > 60:
        raw = boto3.client("secretsmanager", region_name=REGION).get_secret_value(
            SecretId=os.environ["KIRO_SECRET_ID"])["SecretString"]
        _secret_cache.update(value=raw, read_at=time.time())
    return _secret_cache["value"]


def _api_key():
    if os.environ.get("KIRO_API_KEY"):
        return os.environ["KIRO_API_KEY"]
    raw = _secret().strip()
    if raw.startswith("{"):
        return json.loads(raw)["KIRO_API_KEY"]
    return raw


async def _web_token():
    """Access token kept fresh in Secrets Manager by the token refresher Lambda (KIRO_AUTH_MODE=web)."""
    for _ in range(2):
        record = json.loads(await asyncio.to_thread(_secret))
        expires = datetime.fromisoformat(record["expiresAt"].replace("Z", "+00:00"))
        if (expires - datetime.now(timezone.utc)).total_seconds() > 300:
            token = {"accessToken": record["accessToken"], "expiresAt": record["expiresAt"]}
            if record.get("profileArn"):
                token["profileArn"] = record["profileArn"]
            return token
        _secret_cache["value"] = None
    raise RuntimeError("Kiro web login token is expired; run scripts/kiro_web_login.py or check the refresher")


class CredentialsUnavailable(RuntimeError):
    """The Kiro credentials secret is missing, unreadable or expired, for example before the login step ran."""


async def check_credentials():
    """Read the Kiro credentials before any agent starts, so a missing login is reported as such."""
    try:
        if AUTH_MODE == "web":
            await _web_token()
        else:
            await asyncio.to_thread(_api_key)
    except Exception as e:
        raise CredentialsUnavailable(f"Kiro credentials in {os.environ.get('KIRO_SECRET_ID')} are not usable") from e


def _environment():
    home = _home()
    env = {"HOME": str(home), "KIRO_HOME": str(home / ".kiro"), "XDG_DATA_HOME": str(home / ".local" / "share"),
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
           "XDG_STATE_HOME": str(home / ".local" / "state"), "AWS_REGION": REGION,
           # Inherited by the incident-tools MCP servers that Kiro starts for each agent.
           "KNOWLEDGE_DIR": str(KNOWLEDGE_DIR), "REPORTS_TABLE": os.environ.get("REPORTS_TABLE", "incident-to-pr-reports"),
           "INCIDENT_INTERNAL_URL": internal_url(), "INCIDENT_INTERNAL_TOKEN": INTERNAL_TOKEN}
    if AUTH_MODE == "apikey":
        env["KIRO_API_KEY"] = _api_key()
    return env


# ---------------------------------------------------------------- permission policy

def _policy(role):
    subagents, servers = SUBAGENTS.get(role, set()), SERVERS.get(role, set())

    async def decide(name, data, context):
        kiro = context.kiro_metadata or {}
        title = (context.title or "").strip()
        if title.lower().startswith("load skill:"):
            return PermissionResultAllow()  # workspace skills are read-only instructions
        target = None
        if title.lower().startswith("sub-agent:"):
            target = title.split(":", 1)[1].strip()
        elif "subagent" in name.lower():
            target = str((data or {}).get("agent") or (data or {}).get("agent_name") or (data or {}).get("name") or "")
        if target is not None:
            if target in subagents:
                return PermissionResultAllow()
            logger.warning("denied subagent %s for role %s", target, role)
            return PermissionResultDeny(message=f"Subagent {target} is not available to {role}")
        server = None
        for candidate in (title, name, str(kiro.get("toolName") or "")):
            match = re.match(r"@([\w-]+)/", candidate)
            if match:
                server = match.group(1)
                break
        if server in servers:
            return PermissionResultAllow()
        logger.warning("denied tool %r title %r for role %s", name, title, role)
        return PermissionResultDeny(message="Only the incident tools are available")

    return decide


def options(role, extra_env=None, **values):
    """extra_env is added to this session's environment only; Kiro passes it on to the MCP tool servers."""
    base = dict(cwd=str(WORKSPACE), agent_engine="v3", agent=role, model=MODEL, turn_timeout=TURN_TIMEOUT,
                startup_timeout=180, env={**_environment(), **(extra_env or {})}, can_use_tool=_policy(role))
    if AUTH_MODE == "web":
        base.update(kiro_auth_method="client", kiro_access_token_provider=_web_token, cli_path="kiro-cli-chat")
    base.update(values)
    return KiroAgentOptions(**base)


async def response(client):
    result = None
    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        raise RuntimeError("Kiro returned no result")
    if result.is_error:
        raise RuntimeError(f"Kiro turn failed: {result.stop_reason}")
    return result.result or ""


async def ask(role, prompt, extra_env=None, **values):
    """Run one prompt on a fresh Kiro session of the given agent profile and return the final text."""
    prepare()
    async with KiroSDKClient(options(role, extra_env, **values)) as client:
        await client.query(prompt)
        return await response(client)
