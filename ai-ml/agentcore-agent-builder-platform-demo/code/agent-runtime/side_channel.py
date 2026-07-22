import json
import logging
import time
from decimal import Decimal
from typing import Any
from ulid import ULID


logger = logging.getLogger(__name__)

TTL_DAYS = 7


class SideChannelWriter:
    """DynamoDB Side-Channel 이벤트 기록. Spec Section 3.3."""

    def __init__(self, table: Any, session_id: str, agent_id: str):
        self.table = table
        self.session_id = session_id
        self.agent_id = agent_id

    def write_event(self, event_type: str, data: dict) -> str:
        ulid = str(ULID())
        expires_at = int(time.time()) + (TTL_DAYS * 86400)
        item = json.loads(
            json.dumps(
                {
                    "PK": f"SESSION#{self.session_id}",
                    "SK": f"EVENT#{ulid}",
                    "type": event_type,
                    "data": data,
                    "agentId": self.agent_id,
                    "expiresAt": expires_at,
                },
                default=str,
            ),
            parse_float=Decimal,
        )
        try:
            self.table.put_item(Item=item)
        except Exception:
            # Fire-and-forget: Side-Channel 기록 실패가 Agent 실행을 중단시키지 않는다
            logger.exception("Side-channel write failed for EVENT#%s", ulid)
        return ulid
