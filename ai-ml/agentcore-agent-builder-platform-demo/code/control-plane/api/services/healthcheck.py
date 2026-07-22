"""Healthcheck Reconciler — 30초 주기로 AgentCore Runtime 상태를 DDB CONFIG에 기입."""

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

HEALTHCHECK_INTERVAL = 30


async def reconcile_once(agentcore_client, db_service):
    """1회 reconciliation 실행."""
    try:
        runtimes = agentcore_client.control_client.list_agent_runtimes()
        runtime_map = {}
        for rt in runtimes.get("agentRuntimes", []):
            env_vars = {}
            try:
                detail = agentcore_client.control_client.get_agent_runtime(
                    agentRuntimeId=rt["agentRuntimeId"]
                )
                env_vars = detail.get("environmentVariables", {})
            except Exception:
                pass
            agent_id = env_vars.get("AGENT_ID", "")
            if agent_id:
                runtime_map[agent_id] = rt.get("status", "NOT_DEPLOYED")

        agents = db_service.list_agents()
        now = datetime.now(timezone.utc).isoformat()
        updated = 0
        for agent in agents:
            agent_id = agent.get("agentId") or agent["PK"].split("#", 1)[1]
            healthiness = runtime_map.get(agent_id, "NOT_DEPLOYED")
            db_service.update_healthiness(agent_id, healthiness, now)
            updated += 1

        logger.info(
            f"Healthcheck reconciled {updated} agents, {len(runtime_map)} runtimes found"
        )
    except Exception as e:
        logger.error(f"Healthcheck reconciliation failed: {e}")


async def start_reconciler(agentcore_client, db_service):
    """Background task — 시작 시 즉시 1회 실행 후 30초 주기 반복."""
    logger.info("Healthcheck Reconciler started")
    while True:
        await reconcile_once(agentcore_client, db_service)
        await asyncio.sleep(HEALTHCHECK_INTERVAL)
