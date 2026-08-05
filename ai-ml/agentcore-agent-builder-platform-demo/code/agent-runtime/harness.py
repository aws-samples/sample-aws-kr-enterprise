"""Tier 2 Agent Harness -- Pre/Post Hooks, delegation 검증. Spec Section 2.3, 4.2.

NOTE(HITL): 이 런타임은 실제 HITL(사람 승인/일시정지) 게이트를 구현하지 않는다.
과거의 requires_hitl()/hitlActions는 어떤 요청 경로에서도 호출되지 않는 dead
config였다. 승인이 실행을 막는다는 오해를 없애기 위해 제거했다. 승인 게이트를
실제로 도입하려면 agent_runner의 tool 실행 경로에 명시적 gate를 연결해야 한다."""


class Tier2Harness:
    def __init__(self, config: dict, max_depth: int = 2):
        self.config = config
        self.harness_config = config.get("harness", {})
        self.pre_hooks = self.harness_config.get("preHooks", [])
        self.post_hooks = self.harness_config.get("postHooks", [])
        self.delegations = config.get("delegations", [])
        self.allowed_targets = {d["targetAgent"] for d in self.delegations}
        self.max_depth = max_depth

        all_tools: set[str] = set()
        for gw in config.get("gateways", []):
            tf = gw.get("toolFilter", "all")
            if tf != "all":
                all_tools.update(tf)
        self.allowed_tools = all_tools

    def pre_hook_check(self, tool_name: str, action_type: str) -> bool:
        if "scope-validation" not in self.pre_hooks:
            return True
        if action_type == "tool_call" and self.allowed_tools:
            return tool_name in self.allowed_tools
        return True

    def check_delegation_allowed(self, target_agent_id: str) -> bool:
        return target_agent_id in self.allowed_targets

    def check_depth(self, current_depth: int) -> bool:
        return current_depth <= self.max_depth
