"""Agent Lifecycle CRUD. Spec Section 3.2."""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from ulid import ULID

from models.agent import AgentCreateRequest, AgentUpdateRequest

router = APIRouter(prefix="/api/agents", tags=["agents"])


def get_db(request: Request):
    return request.app.state.db_service


def get_harness(request: Request):
    return request.app.state.harness


def get_agentcore(request: Request):
    return request.app.state.agentcore_client


@router.post("")
async def create_agent(req: AgentCreateRequest, db=Depends(get_db)):
    import re

    safe_name = re.sub(
        r"[^a-z0-9-]", "", req.name.lower().replace(" ", "-").replace("&", "and")
    )
    agent_id = f"{safe_name}-{str(ULID())[:8]}"
    config = req.model_dump()
    config["agentId"] = agent_id
    config["version"] = 1
    if not config.get("harness") or not config["harness"].get("preHooks"):
        config["harness"] = {
            "preHooks": ["scope-validation", "persona-injection"],
            "postHooks": ["evaluator"],
            "hitlActions": [],
            "evaluator": {"enabled": True, "criteria": "accuracy,completeness"},
        }
    if not config.get("model"):
        config["model"] = "global.anthropic.claude-sonnet-4-6"
    db.create_agent_config(agent_id, config)
    db.create_agent_card(
        agent_id,
        {
            "name": config.get("name", ""),
            "description": config.get("contextBoundary", ""),
            "capabilities": [gw.get("gatewayId") for gw in config.get("gateways", [])],
            "status": "DEFINED",
            "delegatesTo": [
                d.get("targetAgent") for d in config.get("delegations", [])
            ],
            "contextBoundary": config.get("contextBoundary", ""),
        },
    )
    return {"agentId": agent_id, "status": "defined"}


@router.get("")
async def list_agents(db=Depends(get_db)):
    agents = db.list_agents()
    return {"agents": agents, "count": len(agents)}


@router.get("/{agent_id}")
async def get_agent(agent_id: str, db=Depends(get_db)):
    config = db.get_agent_config(agent_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
    runtime = db.get_runtime_status(agent_id)
    return {"config": config, "runtime": runtime}


@router.put("/{agent_id}")
async def update_agent(agent_id: str, req: AgentUpdateRequest, db=Depends(get_db)):
    updates = req.model_dump(exclude_none=True)
    updated = db.update_agent_config(agent_id, updates)
    return updated


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, db=Depends(get_db)):
    db.delete_agent(agent_id)
    db.unregister_from_supervisor(agent_id)
    return {"agentId": agent_id, "status": "deleted"}


@router.post("/{agent_id}/deploy")
async def deploy_agent(
    agent_id: str,
    db=Depends(get_db),
    harness_svc=Depends(get_harness),
    ac=Depends(get_agentcore),
):
    config = db.get_agent_config(agent_id)
    if not config:
        raise HTTPException(status_code=404, detail="Agent not found")

    gate_result = harness_svc.deploy_quality_gate(config)
    if not gate_result["passed"]:
        raise HTTPException(
            status_code=400,
            detail=f"Deploy quality gate failed: {', '.join(gate_result['errors'])}",
        )

    if not os.environ.get("AGENTCORE_ROLE_ARN"):
        raise HTTPException(
            status_code=500,
            detail="AGENTCORE_ROLE_ARN environment variable is not set",
        )

    runtime = db.get_runtime_status(agent_id)
    if (
        runtime
        and runtime.get("status") in ("READY", "CREATING")
        and runtime.get("runtimeArn")
    ):
        resolved_arn = runtime["runtimeArn"]
    else:
        if not db.claim_runtime_creating(agent_id):
            runtime = db.get_runtime_status(agent_id)
            if runtime and runtime.get("runtimeArn"):
                resolved_arn = runtime["runtimeArn"]
            else:
                raise HTTPException(
                    status_code=409,
                    detail="Deploy already in progress (concurrent request)",
                )
        else:
            image_uri = os.environ.get("BASE_IMAGE_URI")
            if not image_uri:
                raise HTTPException(
                    status_code=500,
                    detail="BASE_IMAGE_URI environment variable is not set",
                )
            resolved_arn = ""
            try:
                resolved_arn = ac.create_runtime(agent_id, image_uri)
                db.update_runtime_status(agent_id, "CREATING", resolved_arn)
                db.update_healthiness(
                    agent_id,
                    "CREATING",
                    datetime.now(timezone.utc).isoformat(),
                )

                runtime_status = ac.wait_for_runtime_ready(resolved_arn, timeout=120)
                if runtime_status != "READY":
                    db.update_runtime_status(
                        agent_id,
                        runtime_status,
                        resolved_arn,
                        failure_reason=f"runtime_{runtime_status.lower()}",
                    )
                    db.update_healthiness(
                        agent_id,
                        runtime_status,
                        datetime.now(timezone.utc).isoformat(),
                    )
                    if runtime_status == "TIMEOUT":
                        raise HTTPException(
                            status_code=504,
                            detail="Runtime provisioning timed out (120s)",
                        )
                    raise HTTPException(
                        status_code=500,
                        detail=f"Runtime provisioning failed: {runtime_status}",
                    )

                db.update_runtime_status(agent_id, "READY", resolved_arn)
            except HTTPException:
                raise
            except Exception as e:
                db.update_runtime_status(
                    agent_id,
                    "CREATE_FAILED",
                    resolved_arn,
                    failure_reason=str(e),
                )
                db.update_healthiness(
                    agent_id,
                    "CREATE_FAILED",
                    datetime.now(timezone.utc).isoformat(),
                )
                raise HTTPException(status_code=500, detail=str(e))

    card = {
        "name": config.get("name", ""),
        "description": config.get("contextBoundary", ""),
        "capabilities": [gw.get("gatewayId") for gw in config.get("gateways", [])],
        "status": "READY",
        "delegatesTo": [d.get("targetAgent") for d in config.get("delegations", [])],
        "contextBoundary": config.get("contextBoundary", ""),
    }
    db.create_agent_card(agent_id, card)
    db.register_with_supervisor(agent_id, {**card, "runtimeArn": resolved_arn})

    harness_svc.audit_log("deploy", agent_id)
    return {"agentId": agent_id, "status": "READY", "runtimeArn": resolved_arn}


@router.post("/{agent_id}/undeploy")
async def undeploy_agent(agent_id: str, db=Depends(get_db), ac=Depends(get_agentcore)):
    runtime = db.get_runtime_status(agent_id)
    if runtime and runtime.get("runtimeArn"):
        try:
            ac.delete_runtime(runtime["runtimeArn"])
        except Exception:
            pass
    db.update_runtime_status(agent_id, "DELETING")
    return {"agentId": agent_id, "status": "DELETING"}


@router.get("/{agent_id}/status")
async def get_agent_status(agent_id: str, db=Depends(get_db)):
    runtime = db.get_runtime_status(agent_id) or {}
    return {"status": runtime.get("status", "unknown")}
