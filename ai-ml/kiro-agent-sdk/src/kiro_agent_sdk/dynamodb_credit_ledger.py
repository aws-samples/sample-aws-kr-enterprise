"""Fail-closed reported-credit accounting in a single DynamoDB region."""
from __future__ import annotations

import asyncio
import re
import uuid
from decimal import Decimal, localcontext
from typing import Any, Optional, Union

from .credit_ledger import _amount, _scope
from .errors import ReportedCreditLimitError

_REVISION = re.compile(r"^[0-9a-f]{32}$")


class DynamoDBCreditLedger:
    """Coordinate reported Kiro credits across clients using conditional writes.

    The dedicated table must have a string partition key named ``scope`` and no
    sort key. All writers must use this ledger in one DynamoDB region. A pending
    or uncertain turn never expires automatically and needs reconciliation.
    """

    def __init__(self, table_name: str, *, region_name: Optional[str] = None,
                 client: Optional[Any] = None, max_conflicts: int = 16) -> None:
        if not isinstance(table_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table_name):
            raise ValueError("Invalid DynamoDB table name")
        if isinstance(max_conflicts, bool) or not isinstance(max_conflicts, int) or not 1 <= max_conflicts <= 100:
            raise ValueError("max_conflicts must be an integer between 1 and 100")
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("dynamodb", region_name=region_name,
                                  config=Config(retries={"total_max_attempts": 1}))
        elif region_name is not None:
            raise ValueError("Specify region_name or client, not both")
        self.table_name, self.client, self.max_conflicts = table_name, client, max_conflicts

    @staticmethod
    def _conditional(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        return (isinstance(response, dict) and isinstance(response.get("Error"), dict) and
                response["Error"].get("Code") == "ConditionalCheckFailedException")

    @staticmethod
    def _string(item: dict[str, Any], key: str) -> str:
        value = item.get(key)
        if not isinstance(value, dict) or set(value) != {"S"} or not isinstance(value["S"], str):
            raise ValueError("Invalid DynamoDB credit ledger item")
        return value["S"]

    def _read(self, scope: str) -> Optional[dict[str, Any]]:
        response = self.client.get_item(TableName=self.table_name, Key={"scope": {"S": scope}},
                                        ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            return None
        if not isinstance(item, dict) or set(item) - {"scope", "limit_value", "reported", "state",
                                                      "reservation_id", "revision"}:
            raise ValueError("Invalid DynamoDB credit ledger item")
        if self._string(item, "scope") != scope:
            raise ValueError("Invalid DynamoDB credit ledger scope")
        limit = _amount(self._string(item, "limit_value"), positive=True)
        total = _amount(self._string(item, "reported"))
        state, revision = self._string(item, "state"), self._string(item, "revision")
        if state not in ("ready", "pending", "unknown") or not _REVISION.fullmatch(revision):
            raise ValueError("Invalid DynamoDB credit ledger state")
        reservation = self._string(item, "reservation_id") if "reservation_id" in item else None
        if ((state == "ready" and reservation is not None) or
                (state != "ready" and (reservation is None or not _REVISION.fullmatch(reservation)))):
            raise ValueError("Invalid DynamoDB credit ledger reservation")
        return {"scope": scope, "limit": limit, "reported": total, "state": state,
                "reservation_id": reservation, "revision": revision}

    def _write(self, scope: str, limit: Decimal, total: Decimal, state: str,
               reservation: Optional[str], previous: Optional[dict[str, Any]]) -> bool:
        item = {
            "scope": {"S": scope}, "limit_value": {"S": str(limit)}, "reported": {"S": str(total)},
            "state": {"S": state}, "revision": {"S": uuid.uuid4().hex},
        }
        if reservation is not None:
            item["reservation_id"] = {"S": reservation}
        params: dict[str, Any] = {"TableName": self.table_name, "Item": item}
        if previous is None:
            params.update(ConditionExpression="attribute_not_exists(#scope)",
                          ExpressionAttributeNames={"#scope": "scope"})
        else:
            params.update(ConditionExpression="#revision = :revision",
                          ExpressionAttributeNames={"#revision": "revision"},
                          ExpressionAttributeValues={":revision": {"S": previous["revision"]}})
        try:
            self.client.put_item(**params)
            return True
        except Exception as exc:
            if self._conditional(exc):
                return False
            raise

    @staticmethod
    def _reservation(value: str) -> str:
        if not isinstance(value, str) or not _REVISION.fullmatch(value):
            raise ValueError("Invalid credit reservation")
        return value

    async def begin(self, scope: str, limit: Decimal) -> tuple[str, Decimal]:
        return await asyncio.to_thread(self._begin, _scope(scope), _amount(limit, positive=True))

    def _begin(self, scope: str, limit: Decimal) -> tuple[str, Decimal]:
        reservation = uuid.uuid4().hex
        for _ in range(self.max_conflicts):
            current = self._read(scope)
            if current is None:
                total = Decimal(0)
            else:
                total = current["reported"]
                if current["limit"] != limit:
                    raise ValueError("Credit scope already has a different limit")
                if current["state"] != "ready":
                    raise ReportedCreditLimitError("Credit usage is pending or unknown; reconcile the ledger",
                                                   float(total), float(limit))
                if total >= limit:
                    raise ReportedCreditLimitError("Reported Kiro credit limit reached", float(total), float(limit))
            if self._write(scope, limit, total, "pending", reservation, current):
                return reservation, total
        raise RuntimeError("Credit ledger update conflicted repeatedly")

    async def complete(self, scope: str, reservation_id: str, usage: Optional[Decimal]) -> Decimal:
        return await asyncio.to_thread(self._complete, _scope(scope), self._reservation(reservation_id),
                                       None if usage is None else _amount(usage))

    def _complete(self, scope: str, reservation: str, usage: Optional[Decimal]) -> Decimal:
        for _ in range(self.max_conflicts):
            current = self._read(scope)
            if current is None or current["state"] != "pending" or current["reservation_id"] != reservation:
                raise ValueError("Credit reservation is no longer pending")
            total = current["reported"]
            if usage is None:
                state, next_reservation = "unknown", reservation
            else:
                with localcontext() as context:
                    context.prec = 1000
                    total += usage
                _amount(total)
                state, next_reservation = "ready", None
            if self._write(scope, current["limit"], total, state, next_reservation, current):
                return total
        raise RuntimeError("Credit ledger update conflicted repeatedly")

    async def inspect(self, scope: str) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self._inspect, _scope(scope))

    def _inspect(self, scope: str) -> Optional[dict[str, Any]]:
        current = self._read(scope)
        if current is None:
            return None
        return {"scope": scope, "limit": str(current["limit"]), "reported_credits": str(current["reported"]),
                "state": current["state"], "reservation_id": current["reservation_id"]}

    async def set_limit(self, scope: str, *, expected_limit: Union[int, float, str, Decimal],
                        new_limit: Union[int, float, str, Decimal]) -> None:
        await asyncio.to_thread(self._set_limit, _scope(scope), _amount(expected_limit, positive=True),
                                _amount(new_limit, positive=True))

    def _set_limit(self, scope: str, expected: Decimal, limit: Decimal) -> None:
        for _ in range(self.max_conflicts):
            current = self._read(scope)
            if current is None or current["state"] != "ready" or current["limit"] != expected:
                raise ValueError("Credit scope is not settled or its limit changed")
            if self._write(scope, limit, current["reported"], "ready", None, current):
                return
        raise RuntimeError("Credit ledger update conflicted repeatedly")

    async def reconcile(self, scope: str, reservation_id: str, *, reported_credits: Union[int, float, str, Decimal],
                        limit: Optional[Union[int, float, str, Decimal]] = None) -> None:
        await asyncio.to_thread(self._reconcile, _scope(scope), self._reservation(reservation_id),
                                _amount(reported_credits), None if limit is None else _amount(limit, positive=True))

    def _reconcile(self, scope: str, reservation: str, total: Decimal, limit: Optional[Decimal]) -> None:
        for _ in range(self.max_conflicts):
            current = self._read(scope)
            if current is None or current["state"] not in ("pending", "unknown") or current["reservation_id"] != reservation:
                raise ValueError("Credit reservation changed or is not uncertain")
            if total < current["reported"]:
                raise ValueError("Reconciled usage cannot decrease")
            if self._write(scope, limit if limit is not None else current["limit"], total, "ready", None, current):
                return
        raise RuntimeError("Credit ledger update conflicted repeatedly")
