from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import aiosqlite

logger = logging.getLogger(__name__)

# Bump on any change to `SCHEMA`. A spool file written by a different version
# is discarded rather than migrated - see `_apply_schema`.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    queued_at REAL NOT NULL,
    topic     TEXT NOT NULL,
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_queued_at ON outbox (queued_at);
"""


@dataclass(frozen=True)
class SpooledMessage:
    """One message waiting for the uplink, with the topic it was meant for."""

    id: int
    queued_at: float
    topic: str
    payload: str


class Spool:
    """
    A durable FIFO queue of MQTT messages, backed by one SQLite file.

    SQLite needs no daemon and commits transactionally - so a message that was
    accepted here is still here after the battery is pulled mid-drive, which is
    exactly when the uplink is down and the samples matter most.

    Async, through aiosqlite: every statement runs on the connection's own
    worker thread, so an fsync on a slow SD card never stalls the event loop
    the rest of the API runs on. The file format is unchanged from the
    threaded version, so an existing spool carries over.

    Delivered rows are deleted rather than flagged `sent`. A delivered message
    is already durable in Postgres, and the failure this guards against is a
    full SD card taking the whole vehicle down: what has to be bounded is the
    file, not the history.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_rows: int = 10_000,
        max_age_seconds: float = 7 * 24 * 3600,
        time_func: Callable[[], float] = time.time,
    ) -> None:
        self.path = str(path)
        self.max_rows = max_rows
        self.max_age_seconds = max_age_seconds
        self._now = time_func
        # Each method below is several statements and a commit; interleaved
        # with another's, one commit would take the other's half-done work.
        self._lock = asyncio.Lock()
        self._connection: aiosqlite.Connection | None = None
        self.dropped = 0

    # -- lifecycle ---------------------------------------------------------

    async def _connect(self) -> aiosqlite.Connection:
        """Opened on first use so constructing a publisher touches no disk."""
        if self._connection is not None:
            return self._connection
        if self.path not in (":memory:", ""):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        # WAL survives a crash mid-write; synchronous=FULL means a commit has
        # reached the card before we call the message ours. At one message
        # every few seconds the extra fsync costs nothing worth counting.
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA synchronous=FULL")
        await self._apply_schema(connection)
        self._connection = connection
        return connection

    @staticmethod
    async def _apply_schema(connection: aiosqlite.Connection) -> None:
        """
        Create the table, discarding a spool written by another version.

        `CREATE TABLE IF NOT EXISTS` silently keeps an older table, so a
        changed schema would only surface as a failing INSERT on a rover that
        already had a spool - at the moment the spool matters most. The stored
        `user_version` is checked instead, and a mismatch drops the table.

        Throwing the backlog away on upgrade is the right trade here and not a
        compromise: the spool is a capped buffer of messages that are already
        in Postgres or a few minutes from it, so the cost is a small gap in
        history. Anything that has to survive a schema change does not belong
        in it.
        """
        async with connection.execute("PRAGMA user_version") as cursor:
            version = int((await cursor.fetchone())[0])
        if version != SCHEMA_VERSION:
            if version != 0:
                logger.warning(
                    "Telemetry spool schema is v%d, expected v%d - discarding the backlog",
                    version,
                    SCHEMA_VERSION,
                )
            await connection.execute("DROP TABLE IF EXISTS outbox")
        # `executescript` commits whatever is open, so this is not one
        # transaction. It does not need to be: the version is stamped last, so
        # a crash part-way leaves the old version and the next open repeats
        # the whole thing.
        await connection.executescript(SCHEMA)
        # No placeholders in a PRAGMA; the value is our own constant.
        await connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        await connection.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._connection is None:
                return
            await self._connection.close()
            self._connection = None

    # -- queue ---------------------------------------------------------------

    async def append(self, topic: str, payload: str) -> int:
        """Store one message and return its id. Trims to the retention cap."""
        async with self._lock:
            connection = await self._connect()
            try:
                cursor = await connection.execute(
                    "INSERT INTO outbox (queued_at, topic, payload) VALUES (?, ?, ?)",
                    (self._now(), topic, payload),
                )
                await self._trim(connection)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            return int(cursor.lastrowid)

    async def pending(self, limit: int) -> list[SpooledMessage]:
        """The oldest `limit` messages. Order is the order they were queued."""
        async with self._lock:
            connection = await self._connect()
            async with connection.execute(
                "SELECT id, queued_at, topic, payload FROM outbox ORDER BY id LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            SpooledMessage(
                id=row["id"],
                queued_at=row["queued_at"],
                topic=row["topic"],
                payload=row["payload"],
            )
            for row in rows
        ]

    async def discard(self, ids: Sequence[int]) -> None:
        """Forget messages the broker has acknowledged."""
        if not ids:
            return
        async with self._lock:
            connection = await self._connect()
            try:
                await connection.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def depth(self) -> int:
        """How many messages are still waiting."""
        async with self._lock:
            connection = await self._connect()
            async with connection.execute("SELECT COUNT(*) FROM outbox") as cursor:
                return int((await cursor.fetchone())[0])

    # -- retention -------------------------------------------------------------

    async def _trim(self, connection: aiosqlite.Connection) -> None:
        """Drop the oldest messages once the spool is too old or too long.

        Newest-wins: during a long outage the recent samples are the ones that
        explain what went wrong, and an unbounded spool fills the card.
        """
        dropped = 0
        if self.max_age_seconds > 0:
            cutoff = self._now() - self.max_age_seconds
            cursor = await connection.execute("DELETE FROM outbox WHERE queued_at < ?", (cutoff,))
            dropped += cursor.rowcount
        if self.max_rows > 0:
            cursor = await connection.execute(
                "DELETE FROM outbox WHERE id NOT IN "
                "(SELECT id FROM outbox ORDER BY id DESC LIMIT ?)",
                (self.max_rows,),
            )
            dropped += cursor.rowcount
        if dropped > 0:
            self.dropped += dropped
            logger.warning(
                "Telemetry spool full: dropped %d oldest message(s) (%d total)",
                dropped,
                self.dropped,
            )
