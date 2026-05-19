from typing import Any


def load_config(agent_id: str, table: Any) -> dict:
    """DynamoDB에서 AGENT#{agent_id}/CONFIG 항목을 로드한다.
    Config은 시작 시 1회만 로드 (immutable). Spec Section 5.6."""
    response = table.get_item(Key={"PK": f"AGENT#{agent_id}", "SK": "CONFIG"})
    item = response.get("Item")
    if not item:
        raise ValueError(f"Agent config not found: {agent_id}")
    return item
