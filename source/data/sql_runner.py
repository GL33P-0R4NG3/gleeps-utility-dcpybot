"""Bounded, transactional SQL execution against only the bot's database."""
import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time


@dataclass
class SQLResult:
    columns: list[str]
    rows: list[list]
    changes: int
    truncated: bool
    backup: str | None


def run_sql(path: str, statement: str, *, seconds: float = 3.0) -> SQLResult:
    if not statement.strip() or len(statement) > 10_000:
        raise ValueError("Enter one SQL statement, up to 10,000 characters.")
    readonly_pragmas = {"table_info", "table_xinfo", "table_list", "index_list", "index_info",
                        "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check"}
    read_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                    sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_PRAGMA}
    blocked = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_TRANSACTION,
               sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE}
    mutating = False

    def authorize(action, arg1, arg2, database, trigger):
        nonlocal mutating
        if action in blocked or (database is not None and database not in ("main", "temp")):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() not in readonly_pragmas:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {"load_extension", "readfile", "writefile"}:
            return sqlite3.SQLITE_DENY
        if action not in read_actions:
            mutating = True
        return sqlite3.SQLITE_OK

    with closing(sqlite3.connect(path, timeout=5)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20_000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        conn.set_authorizer(authorize)
        deadline = time.monotonic() + seconds
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        # EXPLAIN compiles without executing: SQLite identifies writes, including
        # WITH ... DELETE and trigger effects, rather than guessing from SQL text.
        conn.execute("EXPLAIN " + statement).close()
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
        backup = None
        try:
            conn.execute("BEGIN IMMEDIATE" if mutating else "BEGIN")
            if mutating:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup_path = Path(f"{path}.sql-backup-{stamp}.db")
                # A separate reader can back up while this connection reserves the
                # writer lock. Backing up the writer itself would wait on itself.
                with closing(sqlite3.connect(path, timeout=5)) as source:
                    with closing(sqlite3.connect(backup_path)) as target:
                        source.backup(target)
                backup = backup_path.name
            conn.set_authorizer(authorize)
            deadline = time.monotonic() + seconds
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            before = conn.total_changes
            with closing(conn.execute(statement)) as cur:
                columns = [item[0] for item in cur.description] if cur.description else []
                rows = cur.fetchmany(101) if columns else []
                truncated = len(rows) > 100
                result_rows = []
                budget = 24_000
                for row in rows[:100]:
                    values = []
                    for cell in row:
                        if isinstance(cell, bytes):
                            cell = f"<BLOB: {len(cell)} bytes>"
                        elif isinstance(cell, str) and len(cell) > 500:
                            cell = cell[:500] + "…"
                            truncated = True
                        budget -= len(str(cell))
                        values.append(cell)
                    if budget < 0:
                        truncated = True
                        break
                    result_rows.append(values)
            changes = conn.total_changes - before
            conn.set_authorizer(None)
            conn.set_progress_handler(None, 0)
            conn.commit()
            return SQLResult(columns, result_rows, changes, truncated, backup)
        except Exception:
            conn.set_authorizer(None)
            conn.set_progress_handler(None, 0)
            conn.rollback()
            raise


async def execute_sql(path: str, statement: str) -> SQLResult:
    return await asyncio.to_thread(run_sql, path, statement)
