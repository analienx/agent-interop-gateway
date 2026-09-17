from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .models import DelegationResult, DelegationState, Durability

LONG_RETENTION_SECONDS = 10 * 365 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class StoreClaim:
    outcome: str
    fingerprint: str
    result: DelegationResult | None = None


class ResultStore:
    """Durable result journal and cross-process execution claim store."""

    def __init__(self, path: str, ttl_seconds: int) -> None:
        self.path = Path(path).expanduser()
        self.ttl_seconds = ttl_seconds

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.path.parent.chmod(0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delegations (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    risk TEXT NOT NULL DEFAULT 'read',
                    status TEXT NOT NULL DEFAULT 'complete',
                    result_json TEXT NOT NULL,
                    owner TEXT,
                    lease_until REAL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            self._migrate_columns(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_delegations_expires ON delegations(expires_at)"
            )
            connection.execute(
                "DELETE FROM delegations WHERE expires_at < ? AND risk = 'read' "
                "AND status = 'complete'",
                (time.time(),),
            )
        if os.name != "nt" and self.path.exists():
            self.path.chmod(0o600)

    @staticmethod
    def _migrate_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(delegations)").fetchall()
        }
        migrations = {
            "risk": "ALTER TABLE delegations ADD COLUMN risk TEXT NOT NULL DEFAULT 'read'",
            "status": (
                "ALTER TABLE delegations ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'"
            ),
            "owner": "ALTER TABLE delegations ADD COLUMN owner TEXT",
            "lease_until": "ALTER TABLE delegations ADD COLUMN lease_until REAL",
        }
        for name, statement in migrations.items():
            if name not in columns:
                connection.execute(statement)

    async def claim(
        self,
        delegation_id: str,
        fingerprint: str,
        risk: str,
        owner: str,
        lease_seconds: float = 90,
    ) -> StoreClaim:
        return await asyncio.to_thread(
            self._claim_sync,
            delegation_id,
            fingerprint,
            risk,
            owner,
            lease_seconds,
        )

    def _claim_sync(
        self,
        delegation_id: str,
        fingerprint: str,
        risk: str,
        owner: str,
        lease_seconds: float,
    ) -> StoreClaim:
        now = time.time()
        connection = self._connect()
        try:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, risk, status, result_json, owner, lease_until "
                "FROM delegations WHERE id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                queued = DelegationResult(
                    delegation_id=delegation_id,
                    state=DelegationState.QUEUED,
                    durability=Durability.COMMITTED,
                )
                retention = self.ttl_seconds if risk == "read" else LONG_RETENTION_SECONDS
                connection.execute(
                    """
                    INSERT INTO delegations(
                        id, fingerprint, risk, status, result_json, owner, lease_until,
                        updated_at, expires_at
                    ) VALUES(?, ?, ?, 'claimed', ?, ?, ?, ?, ?)
                    """,
                    (
                        delegation_id,
                        fingerprint,
                        risk,
                        queued.model_dump_json(),
                        owner,
                        now + lease_seconds,
                        now,
                        now + retention,
                    ),
                )
                connection.execute("COMMIT")
                return StoreClaim("acquired", fingerprint)

            stored_fingerprint, stored_risk, status, result_json, stored_owner, lease_until = row
            if str(stored_fingerprint) != fingerprint:
                connection.execute("COMMIT")
                return StoreClaim("conflict", str(stored_fingerprint))
            if status == "complete":
                result = DelegationResult.model_validate_json(result_json).model_copy(
                    update={"durability": Durability.COMMITTED}
                )
                connection.execute("COMMIT")
                return StoreClaim("complete", fingerprint, result)
            if status == "uncertain":
                connection.execute("COMMIT")
                return StoreClaim("uncertain", fingerprint)
            if stored_owner == owner:
                connection.execute(
                    "UPDATE delegations SET lease_until = ?, updated_at = ? WHERE id = ?",
                    (now + lease_seconds, now, delegation_id),
                )
                connection.execute("COMMIT")
                return StoreClaim("acquired", fingerprint)
            if lease_until is not None and float(lease_until) > now:
                connection.execute("COMMIT")
                return StoreClaim("busy", fingerprint)
            if str(stored_risk) != "read" or risk != "read":
                connection.execute(
                    "UPDATE delegations SET status = 'uncertain', owner = NULL, "
                    "lease_until = NULL, updated_at = ? WHERE id = ?",
                    (now, delegation_id),
                )
                connection.execute("COMMIT")
                return StoreClaim("uncertain", fingerprint)

            connection.execute(
                "UPDATE delegations SET owner = ?, lease_until = ?, updated_at = ? WHERE id = ?",
                (owner, now + lease_seconds, now, delegation_id),
            )
            connection.execute("COMMIT")
            return StoreClaim("acquired", fingerprint)
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    async def renew(self, delegation_id: str, owner: str, lease_seconds: float = 90) -> bool:
        return await asyncio.to_thread(self._renew_sync, delegation_id, owner, lease_seconds)

    def _renew_sync(self, delegation_id: str, owner: str, lease_seconds: float) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE delegations SET lease_until = ?, updated_at = ? "
                "WHERE id = ? AND owner = ? AND status = 'claimed'",
                (now + lease_seconds, now, delegation_id, owner),
            )
            return cursor.rowcount == 1

    async def get(self, delegation_id: str) -> tuple[str, DelegationResult] | None:
        return await asyncio.to_thread(self._get_sync, delegation_id)

    def _get_sync(self, delegation_id: str) -> tuple[str, DelegationResult] | None:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fingerprint, risk, status, result_json, expires_at, lease_until "
                "FROM delegations WHERE id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                return None
            fingerprint, risk, status, result_json, expires_at, lease_until = row
            if risk == "read" and status == "complete" and float(expires_at) < now:
                connection.execute("DELETE FROM delegations WHERE id = ?", (delegation_id,))
                return None
            if status == "uncertain" or (
                status == "claimed"
                and risk != "read"
                and lease_until is not None
                and float(lease_until) < now
            ):
                if status != "uncertain":
                    connection.execute(
                        "UPDATE delegations SET status = 'uncertain', owner = NULL, "
                        "lease_until = NULL, updated_at = ? WHERE id = ?",
                        (now, delegation_id),
                    )
                return str(fingerprint), DelegationResult(
                    delegation_id=delegation_id,
                    state=DelegationState.FAILED,
                    error=(
                        "previous state-changing delegation has an indeterminate outcome; "
                        "manual resolution is required before retrying"
                    ),
                    durability=Durability.UNCERTAIN,
                )
            return str(fingerprint), DelegationResult.model_validate_json(result_json)

    async def complete(
        self,
        fingerprint: str,
        owner: str,
        result: DelegationResult,
    ) -> bool:
        return await asyncio.to_thread(self._complete_sync, fingerprint, owner, result)

    def _complete_sync(
        self,
        fingerprint: str,
        owner: str,
        result: DelegationResult,
    ) -> bool:
        now = time.time()
        persisted = result.model_copy(update={"durability": Durability.COMMITTED}, deep=True)
        payload = persisted.model_dump_json()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delegations SET
                    status = 'complete', result_json = ?, owner = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE id = ? AND fingerprint = ? AND owner = ? AND status = 'claimed'
                """,
                (payload, now, result.delegation_id, fingerprint, owner),
            )
            return cursor.rowcount == 1

    async def release(self, delegation_id: str, owner: str) -> bool:
        return await asyncio.to_thread(self._release_sync, delegation_id, owner)

    def _release_sync(self, delegation_id: str, owner: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM delegations WHERE id = ? AND owner = ? AND status = 'claimed'",
                (delegation_id, owner),
            )
            return cursor.rowcount == 1

    async def health(self) -> bool:
        return await asyncio.to_thread(self._health_sync)

    def _health_sync(self) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT 1").fetchone()
                return row == (1,)
        except sqlite3.Error:
            return False

    async def prune(self) -> int:
        return await asyncio.to_thread(self._prune_sync)

    def _prune_sync(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM delegations WHERE expires_at < ? AND risk = 'read' "
                "AND status = 'complete'",
                (time.time(),),
            )
            return int(cursor.rowcount)
