"""Tier 1 Platform Harness. Spec Section 2.3.
Deploy Quality Gate, Policy Engine, Audit Log."""

import logging

logger = logging.getLogger(__name__)


class Tier1Harness:
    def __init__(self, db_service):
        self.db = db_service

    def deploy_quality_gate(self, config: dict) -> dict:
        """Phase 3 Deploy 검증: toolFilter ↔ Gateway 카탈로그 대조.
        카탈로그가 미등록 상태이면 검증을 skip한다 (데모 환경 허용)."""
        errors = []
        gateways = config.get("gateways", [])
        missing_tools = self.db.validate_tool_filter(gateways)
        if missing_tools:
            has_catalog = any(
                self.db.get_gateway_tools(gw["gatewayId"])
                for gw in gateways
                if gw.get("toolFilter") != "all"
            )
            if has_catalog:
                errors.extend(
                    [f"Tool not found in catalog: {t}" for t in missing_tools]
                )

        # Validate delegation targets: a config delegating to an unregistered
        # agent id (e.g. a short display name instead of the full agentId)
        # would otherwise deploy clean and fail silently at runtime.
        delegations = config.get("delegations", [])
        missing_targets = self.db.validate_delegations(delegations)
        errors.extend(
            [f"Delegation targetAgent not registered: {t}" for t in missing_targets]
        )

        return {"passed": len(errors) == 0, "errors": errors}

    def audit_log(
        self, action: str, agent_id: str, user_id: str = "", details: dict = None
    ):
        logger.info(
            "AUDIT: action=%s agent=%s user=%s details=%s",
            action,
            agent_id,
            user_id,
            details,
        )

    def check_rate_limit(self, agent_id: str) -> bool:
        return True

    def check_cost_guard(self, agent_id: str) -> bool:
        return True
