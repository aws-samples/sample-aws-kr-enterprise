import json
import logging
import threading
import time
from decimal import Decimal
from typing import Any
from ulid import ULID


logger = logging.getLogger(__name__)

TTL_DAYS = 7

# Monotonic ULID 생성 상태 — 같은 밀리초 내에 발행되는 이벤트도 SK가 엄격히
# 증가하도록 보장한다. 소비자는 EVENT# SK를 오름차순으로 읽으므로, 비단조
# ULID를 쓰면 같은 ms 이벤트의 랜덤 접미사 순서가 뒤섞여 렌더 순서가 어긋난다(L13).
_ulid_lock = threading.Lock()
_last_ulid_int = 0


def _monotonic_ulid() -> str:
    """프로세스 전역에서 엄격히 단조 증가하는 ULID 문자열을 생성한다.
    새 ULID가 직전 값보다 작거나 같으면(같은 ms 충돌) 직전 값 +1을 사용한다."""
    global _last_ulid_int
    with _ulid_lock:
        candidate = int(ULID())
        if candidate <= _last_ulid_int:
            candidate = _last_ulid_int + 1
        _last_ulid_int = candidate
        return str(ULID.from_int(candidate))


class SideChannelWriter:
    """DynamoDB Side-Channel 이벤트 기록. Spec Section 3.3."""

    def __init__(self, table: Any, session_id: str, agent_id: str):
        self.table = table
        self.session_id = session_id
        self.agent_id = agent_id

    def write_event(self, event_type: str, data: dict) -> str:
        ulid = _monotonic_ulid()
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
