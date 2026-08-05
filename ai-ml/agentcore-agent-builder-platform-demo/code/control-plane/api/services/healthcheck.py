"""Healthcheck Reconciler — 30초 주기로 AgentCore Runtime 상태를 DDB CONFIG에 기입."""

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

HEALTHCHECK_INTERVAL = 30

# Runtime statuses that routing (events.py / chat.py / A2A handlers) treats as
# invocable. If AgentCore reports the runtime gone/unhealthy while the RUNTIME
# record still claims one of these, routing would keep dispatching to a dead
# runtime, so we reconcile it below.
ROUTABLE_STATUSES = ("active", "READY")


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

            # Reconcile the RUNTIME item too — routing reads RUNTIME.status, not
            # CONFIG.healthiness (M9). If AgentCore no longer reports the runtime
            # as READY but the RUNTIME record still claims a routable status,
            # demote it so events.py/chat.py/A2A stop dispatching to a dead
            # runtime. The observed AgentCore status is written through verbatim
            # (preserving the existing runtimeArn) so a recovered runtime is
            # promoted back to READY on the next pass.
            runtime = db_service.get_runtime_status(agent_id)
            if not runtime:
                continue
            current_status = runtime.get("status", "")
            if healthiness != current_status and (
                current_status in ROUTABLE_STATUSES
                or healthiness in ROUTABLE_STATUSES
            ):
                db_service.update_runtime_status(
                    agent_id,
                    healthiness,
                    runtime.get("runtimeArn", ""),
                    runtime.get("endpointArn", ""),
                )

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
