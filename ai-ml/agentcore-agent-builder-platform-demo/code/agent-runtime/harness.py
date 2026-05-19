"""Tier 2 Agent Harness -- Pre/Post Hooks, HITL, delegation 검증. Spec Section 2.3, 4.2."""


class Tier2Harness:
    def __init__(self, config: dict, max_depth: int = 2):
        self.config = config
        self.harness_config = config.get("harness", {})
        self.pre_hooks = self.harness_config.get("preHooks", [])
        self.post_hooks = self.harness_config.get("postHooks", [])
        self.hitl_actions = self.harness_config.get("hitlActions", [])
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

    def requires_hitl(self, action: str) -> bool:
        return action in self.hitl_actions
