"""Durable, fail-closed accounting for reported Kiro credits."""
from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Optional, Union

from .errors import ReportedCreditLimitError


def _amount(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("Credit amount must be a finite number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Credit amount must be a finite number") from exc
    if (not amount.is_finite() or amount < 0 or (positive and amount == 0) or
            len(amount.as_tuple().digits) > 400 or abs(amount.as_tuple().exponent) > 400):
        raise ValueError("Credit amount must be a finite nonnegative number")
    return amount


def _scope(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 256 or
            any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError("Credit scope must be a nonempty string of at most 256 characters without control characters")
    return value


class SQLiteCreditLedger:
    """Coordinate reported-credit limits across processes sharing one SQLite file.

    This serializes turns in a scope and persists uncertain turns as pending.
    It cannot see usage outside the participating clients or enforce a USD cap.
    Protect the database directory and use SQLite only on a supported filesystem.
    """

    def __init__(self, path: Union[str, Path], *, timeout: float = 30.0):
        if not isinstance(path, (str, Path)) or not str(path) or str(path) == ":memory:":
            raise ValueError("path must be a persistent SQLite file")
        try:
            valid_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and
                             math.isfinite(timeout) and timeout > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("timeout must be positive")
        self.path, self.timeout = Path(path).expanduser().absolute(), float(timeout)

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise ValueError("Credit ledger path must not be a symlink")
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        connection = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
        connection.execute("""CREATE TABLE IF NOT EXISTS kiro_sdk_credit_limits (
            scope TEXT PRIMARY KEY, limit_value TEXT NOT NULL, reported TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('ready', 'pending', 'unknown')),
            reservation_id TEXT)""")
        return connection

    async def begin(self, scope: str, limit: Decimal) -> tuple[str, Decimal]:
        return await asyncio.to_thread(self._begin, _scope(scope), _amount(limit, positive=True))

    def _begin(self, scope: str, limit: Decimal) -> tuple[str, Decimal]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT limit_value, reported, state FROM kiro_sdk_credit_limits WHERE scope=?",
                             (scope,)).fetchone()
            if row is None:
                total = Decimal(0)
                db.execute("INSERT INTO kiro_sdk_credit_limits VALUES (?, ?, ?, 'ready', NULL)",
                           (scope, str(limit), '0'))
            else:
                saved_limit, total, state = _amount(row[0], positive=True), _amount(row[1]), row[2]
                if saved_limit != limit:
                    raise ValueError("Credit scope already has a different limit")
                if state != "ready":
                    raise ReportedCreditLimitError("Credit usage is pending or unknown; reconcile the ledger",
                                                   float(total), float(limit))
                if total >= limit:
                    raise ReportedCreditLimitError("Reported Kiro credit limit reached", float(total), float(limit))
            reservation = uuid.uuid4().hex
            db.execute("UPDATE kiro_sdk_credit_limits SET state='pending', reservation_id=? WHERE scope=?",
                       (reservation, scope))
            db.commit()
            return reservation, total
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    async def complete(self, scope: str, reservation_id: str, usage: Optional[Decimal]) -> Decimal:
        return await asyncio.to_thread(self._complete, _scope(scope), reservation_id,
                                       None if usage is None else _amount(usage))

    def _complete(self, scope: str, reservation_id: str, usage: Optional[Decimal]) -> Decimal:
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("Invalid credit reservation")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reported, state, reservation_id FROM kiro_sdk_credit_limits WHERE scope=?",
                             (scope,)).fetchone()
            if row is None or row[1] != 'pending' or row[2] != reservation_id:
                raise ValueError("Credit reservation is no longer pending")
            total = _amount(row[0])
            if usage is None:
                db.execute("UPDATE kiro_sdk_credit_limits SET state='unknown' WHERE scope=?", (scope,))
            else:
                with localcontext() as context:
                    context.prec = 1000
                    total += usage
                _amount(total)
                db.execute("UPDATE kiro_sdk_credit_limits SET reported=?, state='ready', reservation_id=NULL WHERE scope=?",
                           (str(total), scope))
            db.commit()
            return total
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    async def inspect(self, scope: str) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self._inspect, _scope(scope))

    def _inspect(self, scope: str) -> Optional[dict[str, Any]]:
        db = self._connect()
        try:
            row = db.execute("SELECT limit_value, reported, state, reservation_id FROM kiro_sdk_credit_limits WHERE scope=?",
                             (scope,)).fetchone()
            if row is None:
                return None
            return {"scope": scope, "limit": str(_amount(row[0], positive=True)),
                    "reported_credits": str(_amount(row[1])), "state": row[2], "reservation_id": row[3]}
        finally:
            db.close()

    async def set_limit(self, scope: str, *, expected_limit: Union[int, float, str, Decimal],
                        new_limit: Union[int, float, str, Decimal]) -> None:
        """Change a settled scope's limit only if its old limit still matches."""
        await asyncio.to_thread(self._set_limit, _scope(scope), _amount(expected_limit, positive=True),
                                _amount(new_limit, positive=True))

    def _set_limit(self, scope: str, expected: Decimal, limit: Decimal) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT limit_value, state FROM kiro_sdk_credit_limits WHERE scope=?",
                             (scope,)).fetchone()
            if row is None or row[1] != "ready" or _amount(row[0], positive=True) != expected:
                raise ValueError("Credit scope is not settled or its limit changed")
            db.execute("UPDATE kiro_sdk_credit_limits SET limit_value=? WHERE scope=?", (str(limit), scope))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    async def reconcile(self, scope: str, reservation_id: str, *, reported_credits: Union[int, float, str, Decimal],
                        limit: Optional[Union[int, float, str, Decimal]] = None) -> None:
        """Resolve a pending/unknown turn after independently checking its final usage."""
        await asyncio.to_thread(self._reconcile, _scope(scope), reservation_id, _amount(reported_credits),
                                None if limit is None else _amount(limit, positive=True))

    def _reconcile(self, scope: str, reservation_id: str, total: Decimal, limit: Optional[Decimal]) -> None:
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("A current reservation ID is required")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT limit_value, reported, state, reservation_id FROM kiro_sdk_credit_limits WHERE scope=?",
                             (scope,)).fetchone()
            if row is None or row[2] not in ('pending', 'unknown') or row[3] != reservation_id:
                raise ValueError("Credit reservation changed or is not uncertain")
            if total < _amount(row[1]):
                raise ValueError("Reconciled usage cannot decrease")
            db.execute("UPDATE kiro_sdk_credit_limits SET limit_value=?, reported=?, state='ready', reservation_id=NULL WHERE scope=?",
                       (str(limit if limit is not None else _amount(row[0], positive=True)), str(total), scope))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
