"""DynamoDB operations for Platform API. Spec Section 5.1."""

from typing import Any, Optional
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key


class DynamoDBService:
    def __init__(self, table: Any):
        self.table = table

    def create_agent_config(self, agent_id: str, config: dict) -> dict:
        config["agentId"] = agent_id
        item = {
            "PK": f"AGENT#{agent_id}",
            "SK": "CONFIG",
            **config,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        self.table.put_item(Item=item)
        return item

    def get_agent_config(self, agent_id: str) -> Optional[dict]:
        response = self.table.get_item(Key={"PK": f"AGENT#{agent_id}", "SK": "CONFIG"})
        return response.get("Item")

    def update_agent_config(self, agent_id: str, updates: dict) -> dict:
        existing = self.get_agent_config(agent_id)
        if not existing:
            raise ValueError(f"Agent not found: {agent_id}")
        existing.update(updates)
        existing["version"] = existing.get("version", 0) + 1
        self.table.put_item(Item=existing)
        return existing

    def delete_agent(self, agent_id: str):
        for sk in ["CONFIG", "RUNTIME", "CARD"]:
            self.table.delete_item(Key={"PK": f"AGENT#{agent_id}", "SK": sk})

    def list_agents(self) -> list[dict]:
        response = self.table.query(
            IndexName="sk-pk-index",
            KeyConditionExpression=Key("SK").eq("CONFIG")
            & Key("PK").begins_with("AGENT#"),
        )
        items = response.get("Items", [])
        for item in items:
            if "agentId" not in item:
                item["agentId"] = item["PK"].split("#", 1)[1]
        return items

    def update_healthiness(self, agent_id: str, healthiness: str, checked_at: str):
        try:
            self.table.update_item(
                Key={"PK": f"AGENT#{agent_id}", "SK": "CONFIG"},
                UpdateExpression="SET healthiness = :h, healthCheckedAt = :t",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":h": healthiness, ":t": checked_at},
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

    def create_agent_card(self, agent_id: str, card: dict):
        item = {
            "PK": f"AGENT#{agent_id}",
            "SK": "CARD",
            "entityType": "AGENT_CARD",
            **card,
        }
        self.table.put_item(Item=item)

    def claim_runtime_creating(self, agent_id: str) -> bool:
        """Conditionally write CREATING status. Returns True if claimed.
        Fails (returns False) if RUNTIME record already exists with
        status CREATING or READY — preventing race condition."""
        try:
            self.table.put_item(
                Item={
                    "PK": f"AGENT#{agent_id}",
                    "SK": "RUNTIME",
                    "status": "CREATING",
                    "runtimeArn": "",
                    "endpointArn": "",
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                },
                ConditionExpression="attribute_not_exists(PK) OR (NOT #s IN (:creating, :ready))",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":creating": "CREATING",
                    ":ready": "READY",
                },
            )
            return True
        except Exception:
            return False

    def update_runtime_status(
        self,
        agent_id: str,
        status: str,
        runtime_arn: str = "",
        endpoint_arn: str = "",
        failure_reason: str = "",
    ):
        item = {
            "PK": f"AGENT#{agent_id}",
            "SK": "RUNTIME",
            "status": status,
            "runtimeArn": runtime_arn,
            "endpointArn": endpoint_arn,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        if failure_reason:
            item["failureReason"] = failure_reason
        self.table.put_item(Item=item)

    def get_runtime_status(self, agent_id: str) -> Optional[dict]:
        response = self.table.get_item(Key={"PK": f"AGENT#{agent_id}", "SK": "RUNTIME"})
        return response.get("Item")

    def register_with_supervisor(self, agent_id: str, card: dict):
        item = {
            "PK": "SUPERVISOR",
            "SK": f"AGENT#{agent_id}",
            "name": card.get("name", ""),
            "contextBoundary": card.get("contextBoundary", ""),
            "capabilities": card.get("capabilities", []),
            "runtimeArn": card.get("runtimeArn", ""),
        }
        self.table.put_item(Item=item)

    def unregister_from_supervisor(self, agent_id: str):
        self.table.delete_item(Key={"PK": "SUPERVISOR", "SK": f"AGENT#{agent_id}"})

    def list_supervisor_agents(self) -> list[dict]:
        response = self.table.query(KeyConditionExpression=Key("PK").eq("SUPERVISOR"))
        return response.get("Items", [])

    def poll_session_events(self, session_id: str, after_sk: str = "") -> list[dict]:
        kwargs = {
            "KeyConditionExpression": Key("PK").eq(f"SESSION#{session_id}")
            & Key("SK").begins_with("EVENT#"),
            "ScanIndexForward": True,
        }
        if after_sk:
            kwargs["ExclusiveStartKey"] = {
                "PK": f"SESSION#{session_id}",
                "SK": after_sk,
            }
        response = self.table.query(**kwargs)
        return response.get("Items", [])

    def create_session(self, session_id: str, agent_id: str, trigger: str = "chat"):
        item = {
            "PK": f"SESSION#{session_id}",
            "SK": "META",
            "agentId": agent_id,
            "trigger": trigger,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "status": "in_progress",
        }
        self.table.put_item(Item=item)

    def list_recent_sessions(self, limit: int = 20) -> list[dict]:
        response = self.table.query(
            IndexName="sk-pk-index",
            KeyConditionExpression=Key("SK").eq("META")
            & Key("PK").begins_with("SESSION#"),
        )
        items = response.get("Items", [])
        for item in items:
            item["sessionId"] = item["PK"].replace("SESSION#", "")
        items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
        return items[:limit]

    def get_session_events(self, session_id: str) -> list[dict]:
        """세션 내 모든 이벤트 조회 (trajectory)."""
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"SESSION#{session_id}")
            & Key("SK").begins_with("EVENT#"),
            ScanIndexForward=True,
        )
        return response.get("Items", [])

    def save_gateway_tool(self, gateway_id: str, tool: dict):
        item = {
            "PK": f"GATEWAY#{gateway_id}",
            "SK": f"TOOL#{tool['name']}",
            **tool,
        }
        self.table.put_item(Item=item)

    def get_gateway_tools(self, gateway_id: str) -> list[dict]:
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"GATEWAY#{gateway_id}")
            & Key("SK").begins_with("TOOL#")
        )
        return response.get("Items", [])

    def list_gateways(self) -> list[dict]:
        response = self.table.query(
            IndexName="sk-pk-index",
            KeyConditionExpression=Key("SK").eq("CONFIG")
            & Key("PK").begins_with("GATEWAY#"),
        )
        return response.get("Items", [])

    def validate_tool_filter(self, gateways: list[dict]) -> list[str]:
        """Quality Gate: toolFilter의 모든 Tool이 카탈로그에 존재하는지 검증."""
        missing = []
        for gw in gateways:
            tool_filter = gw.get("toolFilter", "all")
            if tool_filter == "all":
                continue
            catalog_tools = self.get_gateway_tools(gw["gatewayId"])
            catalog_names = {t.get("name") for t in catalog_tools}
            for tool_name in tool_filter:
                if tool_name not in catalog_names:
                    missing.append(f"{gw['gatewayId']}/{tool_name}")
        return missing
