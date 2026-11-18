"""Shared prerequisites for SQLite stores using UPSERT."""
import sqlite3


def require_upsert() -> None:
    if sqlite3.sqlite_version_info < (3, 24, 0):
        raise RuntimeError("SQLite 3.24 or newer is required for SDK UPSERT stores")
