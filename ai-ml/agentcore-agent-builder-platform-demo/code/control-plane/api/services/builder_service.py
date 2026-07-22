"""Builder Agent — Platform API 내장. Spec Section 4.1 Phase 1.
대화 상태 머신: INIT → INTENT_GATHERING → BOUNDARY_DEFINITION →
TOOL_MATCHING → DELEGATION_CHECK → CONFIG_GENERATION → DONE"""

import asyncio
import json
import os
from typing import AsyncGenerator

import boto3

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")


class BuilderService:
    def __init__(self, db_service):
        self.db = db_service
        self.bedrock = boto3.client("bedrock-runtime", region_name=REGION)
        self._catalog_cache: list[dict] | None = None
        self._cards_cache: list[dict] | None = None
        self._cache_ttl: float = 0.0

    def chat(self, messages: list[dict], session_id: str, state: str = "INIT") -> dict:
        """Builder Agent 대화. 상태에 따라 system prompt를 조정하고 Bedrock Claude를 호출."""
        gateway_catalog = self._load_gateway_catalog()
        existing_cards = self._load_agent_cards()
        system_prompt = self._build_system_prompt(
            state, gateway_catalog, existing_cards
        )

        clean_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]

        response = self.bedrock.invoke_model(
            modelId="global.anthropic.claude-opus-4-6-v1",
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "messages": clean_messages,
                }
            ),
        )

        body = json.loads(response["body"].read())
        assistant_message = body["content"][0]["text"]

        next_state = self._determine_next_state(state, assistant_message)

        result = {
            "message": assistant_message,
            "state": next_state,
            "sessionId": session_id,
        }

        if next_state == "DONE":
            config_json = self._extract_config_json(assistant_message)
            if config_json:
                config_json = self._ensure_complete_config(config_json)
                result["agentConfig"] = config_json

        return result

    async def chat_stream(
        self, messages: list[dict], session_id: str, state: str = "INIT"
    ) -> AsyncGenerator[str, None]:
        """Builder Agent 스트리밍 대화. SSE 형식으로 토큰을 yield한다."""
        try:
            yield f"event: status\ndata: {json.dumps({'phase': state, 'sessionId': session_id}, ensure_ascii=False)}\n\n"

            gateway_catalog = self._load_gateway_catalog()
            existing_cards = self._load_agent_cards()
            system_prompt = self._build_system_prompt(
                state, gateway_catalog, existing_cards
            )

            clean_messages = [
                {"role": m["role"], "content": m["content"]}
                for m in messages
                if m.get("role") in ("user", "assistant")
            ]

            body = json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "messages": clean_messages,
                }
            )

            response = await asyncio.to_thread(
                self.bedrock.invoke_model_with_response_stream,
                modelId="global.anthropic.claude-opus-4-6-v1",
                contentType="application/json",
                accept="application/json",
                body=body,
            )

            full_text = ""
            for event in response.get("body", []):
                chunk_bytes = event.get("chunk", {}).get("bytes", b"")
                if not chunk_bytes:
                    continue
                payload = json.loads(chunk_bytes)
                if payload.get("type") == "content_block_delta":
                    delta = payload.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        full_text += text
                        yield f"event: text\ndata: {json.dumps({'content': text}, ensure_ascii=False)}\n\n"
                elif payload.get("type") == "message_stop":
                    break

            next_state = self._determine_next_state(state, full_text)

            done_data: dict = {
                "state": next_state,
                "sessionId": session_id,
                "fullMessage": full_text,
            }
            if next_state == "DONE":
                config_json = self._extract_config_json(full_text)
                if config_json:
                    config_json = self._ensure_complete_config(config_json)
                    done_data["agentConfig"] = config_json

            yield f"event: done\ndata: {json.dumps(done_data, ensure_ascii=False)}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            return

    def _load_gateway_catalog(self) -> list[dict]:
        import time

        now = time.time()
        if self._catalog_cache is not None and now < self._cache_ttl:
            return self._catalog_cache

        gateways = self.db.list_gateways()
        catalog = []
        for gw in gateways:
            gw_id = gw["PK"].replace("GATEWAY#", "")
            tools = self.db.get_gateway_tools(gw_id)
            catalog.append(
                {
                    "gatewayId": gw_id,
                    "name": gw.get("name", ""),
                    "tools": [
                        {
                            "name": t.get("name"),
                            "description": t.get("description", ""),
                        }
                        for t in tools
                    ],
                }
            )
        self._catalog_cache = catalog
        self._cache_ttl = now + 60
        return catalog

    def _load_agent_cards(self) -> list[dict]:
        import time

        now = time.time()
        if self._cards_cache is not None and now < self._cache_ttl:
            return self._cards_cache

        agents = self.db.list_supervisor_agents()
        cards = [
            {
                "agentId": a["SK"].replace("AGENT#", ""),
                "name": a.get("name", ""),
                "contextBoundary": a.get("contextBoundary", ""),
                "capabilities": a.get("capabilities", []),
            }
            for a in agents
        ]
        self._cards_cache = cards
        return cards

    def _build_system_prompt(self, state: str, catalog: list, cards: list) -> str:
        base = """You are the Builder Agent for the AIOps Multi Agent Platform.
Your job is to help users create Domain Agents by defining their Context Boundary
and assembling the right tools from the Gateway catalog.

## Current State: {state}

## Available Gateway Catalog
{catalog}

## Existing Agent Cards
{cards}

## State Machine Rules
- INIT/INTENT_GATHERING: Ask what kind of agent they want to create. Ask in Korean.
- BOUNDARY_DEFINITION: Propose a context boundary based on their intent. Explain in Korean.
- TOOL_MATCHING: Recommend gateways and tools from the catalog. Explain in Korean.
- DELEGATION_CHECK: Check if existing agents overlap, recommend A2A delegation. Explain in Korean.
- CONFIG_GENERATION: Generate the complete Agent Config JSON. Wrap in ```json markers.

## CRITICAL: systemPrompt Field in Generated Config
When you output the Agent Config JSON, the `systemPrompt` field is THE MOST IMPORTANT field.
It MUST NOT be empty. It MUST contain ALL of the following:

1. **Role description** (2-3 sentences): What this agent does, its expertise area
2. **Language rule**: "사용자가 한국어로 질문하면 한국어로 응답하세요."
3. **Scope enforcement rule** (COPY THIS EXACTLY, replacing [BOUNDARY] with the actual contextBoundary):
   "Your Context Boundary is: [BOUNDARY]. You MUST only handle requests within this boundary. For any request outside this boundary, do NOT delegate to other agents and do NOT use any tools. Instead, respond: 'This request is outside my scope ([BOUNDARY]). Please direct it to the appropriate agent.'"
4. **Tool usage instructions**: How to use the assigned gateways and tools

Example systemPrompt (for an EKS Monitoring agent with boundary "EKS Cluster Observability"):
"You are an EKS Cluster Observability specialist agent. You monitor Pod status, node health, and cluster events using Container Gateway tools.\\n\\n사용자가 한국어로 질문하면 한국어로 응답하세요.\\n\\nYour Context Boundary is: EKS Cluster Observability. You MUST only handle requests within this boundary. For any request outside this boundary, do NOT delegate to other agents and do NOT use any tools. Instead, respond: 'This request is outside my scope (EKS Cluster Observability). Please direct it to the appropriate agent.'\\n\\nUse container-gw tools to check pod status (list_pods, describe_pod), node health (describe_node), and cluster events (list_events)."

If the systemPrompt you generate is shorter than 100 characters, you have done it wrong. Regenerate.

## Output Format
When in CONFIG_GENERATION state, output a complete Agent Config JSON matching the schema.
The JSON MUST be wrapped in ```json ... ``` markers."""

        return base.format(
            state=state,
            catalog=json.dumps(catalog, indent=2, ensure_ascii=False),
            cards=json.dumps(cards, indent=2, ensure_ascii=False),
        )

    def _determine_next_state(self, current: str, response: str) -> str:
        """LLM 응답 분석 기반 state 전이. 질문 중이면 현재 state 유지."""
        if "```json" in response:
            return "DONE"

        state_flow = {
            "INIT": "INTENT_GATHERING",
            "INTENT_GATHERING": "BOUNDARY_DEFINITION",
            "BOUNDARY_DEFINITION": "TOOL_MATCHING",
            "TOOL_MATCHING": "DELEGATION_CHECK",
            "DELEGATION_CHECK": "CONFIG_GENERATION",
            "CONFIG_GENERATION": "DONE",
        }

        if current == "INIT":
            return "INTENT_GATHERING"

        trimmed = response.strip()
        if trimmed.endswith("?") or trimmed.endswith('?"') or trimmed.endswith("?\n"):
            return current

        transition_signals = {
            "INTENT_GATHERING": [
                "context boundary",
                "boundary",
                "범위",
                "경계",
                "도메인",
            ],
            "BOUNDARY_DEFINITION": ["gateway", "tool", "도구", "게이트웨이", "API"],
            "TOOL_MATCHING": [
                "delegation",
                "위임",
                "다른 agent",
                "다른 에이전트",
                "협력",
            ],
            "DELEGATION_CHECK": ["config", "설정", "json", "생성", "완료"],
        }

        signals = transition_signals.get(current, [])
        response_lower = response.lower()
        if any(signal in response_lower for signal in signals):
            return state_flow.get(current, current)

        return state_flow.get(current, current)

    def _ensure_complete_config(self, config: dict) -> dict:
        """Builder가 생성한 config의 필수 필드를 보완."""
        if "capabilities" in config and not config.get("gateways"):
            raw_gws = config.pop("capabilities")
            config["gateways"] = [
                gw if isinstance(gw, dict) else {"gatewayId": gw, "toolFilter": "all"}
                for gw in raw_gws
            ]

        config["gateways"] = [
            gw if isinstance(gw, dict) else {"gatewayId": gw, "toolFilter": "all"}
            for gw in config.get("gateways", [])
        ]

        raw_delegations = config.get("delegations", [])
        normalized = []
        for d in raw_delegations:
            target = d.get("targetAgent", d.get("targetAgentId", ""))
            if not target:
                continue
            normalized.append(
                {
                    "targetAgent": target,
                    "purpose": d.get("purpose", ""),
                    "scope": d.get("scope", []),
                    "condition": d.get("condition", "always"),
                    "timeout": d.get("timeout", 90),
                }
            )
        config["delegations"] = normalized

        boundary = config.get("contextBoundary", "General Purpose")

        prompt = config.get("systemPrompt", "")
        if not prompt or len(prompt) < 100:
            config["systemPrompt"] = (
                f"You are a {boundary} specialist agent.\n\n"
                f"사용자가 한국어로 질문하면 한국어로 응답하세요.\n\n"
                f"Your Context Boundary is: {boundary}. "
                f"You MUST only handle requests within this boundary. "
                f"For any request outside this boundary, do NOT delegate to other agents "
                f"and do NOT use any tools. Instead, respond: "
                f"'This request is outside my scope ({boundary}). "
                f"Please direct it to the appropriate agent.'"
            )

        harness = config.get("harness", {})
        if not harness.get("preHooks"):
            config["harness"] = {
                "preHooks": ["scope-validation", "persona-injection"],
                "postHooks": ["evaluator"],
                "hitlActions": harness.get("hitlActions", []),
                "evaluator": {"enabled": True, "criteria": "accuracy,completeness"},
            }

        if not config.get("triggers"):
            config["triggers"] = [
                {
                    "type": "chat",
                    "source": "platform",
                    "description": "Platform UI chat",
                }
            ]

        if not config.get("internalTools"):
            config["internalTools"] = []

        if not config.get("createdBy"):
            config["createdBy"] = "builder"

        if not config.get("version"):
            config["version"] = 1

        return config

    def _extract_config_json(self, text: str) -> dict | None:
        if "```json" not in text:
            return None
        try:
            start = text.index("```json") + 7
            end = text.index("```", start)
        except ValueError:
            return None
        try:
            return json.loads(text[start:end].strip())
        except json.JSONDecodeError:
            return None
