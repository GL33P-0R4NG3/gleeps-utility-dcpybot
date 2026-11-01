# db_helper.py
import aiosqlite
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone

from settings import normalize_settings
from typing import Optional, Dict, Any, AsyncGenerator


class DBHelper:
    """Async wrapper around the SQLite DB used by the bot."""

    def __init__(self, path: str = "utility_bot.db"):
        self.path = path

    @asynccontextmanager
    async def connect(self):
        """Every connection enforces the same referential-integrity rules."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    # -------------------------------------------------------------------------------
    #  Initialize the database – creates all if not present
    # -------------------------------------------------------------------------------
    async def init(self) -> None:
        # Rebuild legacy child tables transactionally; never discard orphaned data.
        async with aiosqlite.connect(self.path) as db:
            legacy = False
            for table in ("users", "voice_channels"):
                async with db.execute(f"PRAGMA foreign_key_list({table})") as cur:
                    keys = await cur.fetchall()
                if any(row[2] == "lobbies" and row[3] == "lobby_id" and row[1] == 0 for row in keys):
                    legacy = True
            if legacy:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = Path(f"{self.path}.backup-{stamp}.db")
                async with aiosqlite.connect(backup) as target:
                    await db.backup(target)
                logging.getLogger(__name__).info("Database backup created: %s", backup)
            await db.execute("PRAGMA foreign_keys = OFF")
            await db.execute("BEGIN IMMEDIATE")
            try:
                if legacy:
                    for table in ("users", "voice_channels"):
                        await db.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
                schema = """
                CREATE TABLE IF NOT EXISTS guilds (
                    guild_id      INTEGER PRIMARY KEY,
                    settings_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS users (
                    guild_id      INTEGER NOT NULL,
                    lobby_id      INTEGER NOT NULL,
                    user_id       INTEGER NOT NULL,
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (guild_id, lobby_id, user_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE,
                    FOREIGN KEY (guild_id, lobby_id) REFERENCES lobbies(guild_id, lobby_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS voice_channels (
                    guild_id        INTEGER NOT NULL,
                    lobby_id        INTEGER NOT NULL,
                    channel_id      INTEGER NOT NULL,
                    owner_id        INTEGER NOT NULL,
                    private         BOOL NOT NULL DEFAULT FALSE,
                    purpose         TEXT NOT NULL DEFAULT 'general',
                    channel_name    TEXT NOT NULL,
                    last_dc_time    INTEGER,
                    save_settings   BOOL NOT NULL DEFAULT TRUE,
                    settings_json   TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (guild_id, lobby_id, channel_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE,
                    FOREIGN KEY (guild_id, lobby_id) REFERENCES lobbies(guild_id, lobby_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS lobbies (
                    guild_id       INTEGER NOT NULL,
                    lobby_id       INTEGER NOT NULL,
                    settings_json  TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (guild_id, lobby_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS semi_lobby_locks (
                    guild_id INTEGER NOT NULL,
                    lobby_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    previous_connect INTEGER,
                    PRIMARY KEY (guild_id, lobby_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS semi_rooms (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    created_role INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER,
                    hidden INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (guild_id, owner_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );
                """
                for statement in schema.split(";"):
                    if statement.strip():
                        await db.execute(statement)
                if legacy:
                    for table in ("users", "voice_channels"):
                        await db.execute(f"INSERT INTO {table} SELECT * FROM {table}_legacy")
                        await db.execute(f"DROP TABLE {table}_legacy")
                async with db.execute("PRAGMA foreign_key_check") as cur:
                    violations = await cur.fetchall()
                if violations:
                    raise RuntimeError(f"Database contains orphaned records; migration rolled back: {violations}")
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    # -------------------------------------------------------------------------------
    #  GUILD helpers
    # -------------------------------------------------------------------------------
    async def get_settings_guild(self, guild_id: int) -> Dict[str, Any]:
        """Return the JSON dict stored in guilds.settings_json. Creates row if missing."""
        async with self.connect() as db:
            async with db.execute(
                "SELECT settings_json FROM guilds WHERE guild_id = ?", (guild_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return normalize_settings(json.loads(row[0]))

                # No row yet – create a default one
                await db.execute("INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)", (guild_id,))
                await db.commit()
                return normalize_settings({})

    async def set_settings_guild(self, guild_id: int, settings: Dict[str, Any]) -> None:
        """
        Replace the whole ``settings_json`` for a guild with the supplied dict.
        """
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO guilds (guild_id, settings_json)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE
                SET settings_json = excluded.settings_json
                """,
                (guild_id, json.dumps(normalize_settings(settings))),
            )
            await db.commit()

    # -------------------------------------------------------------------------------
    #  USER helpers (settings scoped to guild, lobby and user)
    # -------------------------------------------------------------------------------
    async def get_settings_user(self, guild_id: int, lobby_id: int, user_id: int) -> None | Dict[str, Any]:
        """Fetch settings_json for a (guild_id, lobby_id, user_id) key. Returns None if missing."""
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT settings_json FROM users
                WHERE guild_id = ? AND lobby_id = ? AND user_id = ?
                """,
                (guild_id, lobby_id, user_id),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return json.loads(row[0])

                return None

    async def set_settings_user(
            self,
            guild_id: int,
            lobby_id: int,
            user_id: int,
            settings: Dict[str, Any]
    ) -> None:
        """
        Replace settings for a user in a specific lobby.
        If the row does not exist, yet it will be created.
        """
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO users (guild_id, lobby_id, user_id, settings_json)
                VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, lobby_id, user_id) DO
                UPDATE
                    SET settings_json = excluded.settings_json
                """,
                (guild_id, lobby_id, user_id, json.dumps(settings)),
            )
            await db.commit()

    # -------------------------------------------------------------------------------
    #  VOICE‑CHANNEL helpers
    # -------------------------------------------------------------------------------
    async def set_voice_channel(
            self, *,
            channel_id: int,
            guild_id: int,
            lobby_id: int,
            owner_id: int,
            channel_name: str,
            save_settings: bool = True,
            purpose: str = "general",
            private: bool = False,
            extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Insert a new temporary voice‑channel row.
        Returns the SQLite rowid; channel_id is the Discord identifier.
        """
        settings = json.dumps(extra or {})

        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO voice_channels
                (channel_id, guild_id, lobby_id, owner_id, private, purpose,
                 channel_name, save_settings, settings_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    guild_id,
                    lobby_id,
                    owner_id,
                    int(private),
                    purpose,
                    channel_name,
                    save_settings,
                    settings,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_voice_channel(self, guild_id: int, channel_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a voice‑channel row by its Discord ``channel_id``."""
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT *
                FROM voice_channels
                WHERE channel_id = ?
                AND guild_id = ?
                """,
                (channel_id, guild_id),
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None

                cols = [d[0] for d in cur.description]
                data = dict(zip(cols, row))
                # Decode the JSON payload
                data["settings_json"] = json.loads(data.pop("settings_json") or "{}")
                return data

    async def get_count_voice_channel_by_member(self, guild_id: int, member_id: int, lobby_id: int) -> int:
        """Fetch a voice‑channel row by its Discord ``channel_id``."""
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT COUNT(channel_id) FROM voice_channels
                WHERE owner_id = ?
                AND guild_id = ?
                AND lobby_id = ?
                """,
                (member_id, guild_id, lobby_id),
            ) as cur:
                row = await cur.fetchone()
                return row[0]

    async def get_count_voice_channels(self, guild_id: int, lobby_id: int) -> int:
        """
        :param guild_id:
        :param lobby_id:
        :return: The number of active voice channels in given guild.
        """
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT COUNT(channel_id) FROM voice_channels
                WHERE guild_id = ?
                AND lobby_id = ?
                """,
                (guild_id, lobby_id)
            ) as cur:
                row = await cur.fetchone()
                return row[0]

    async def set_voice_channel_settings(
        self, channel_id: int, settings: Dict[str, Any]
    ) -> None:
        """
        Replace the whole ``settings_json`` for a temporary voice channel.
        """
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE voice_channels
                SET settings_json = ?
                WHERE channel_id = ?
                """,
                (json.dumps(settings), channel_id),
            )
            await db.commit()

    async def update_voice_last_disconnect(
        self, guild_id: int, channel_id: int, timestamp: Optional[int] = None
    ) -> None:
        """
        Update the ``last_dc_time`` column (used for expiry or “when did it close”).
        If ``timestamp`` is omitted, the expiry is cleared.
        """
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE voice_channels
                SET last_dc_time = ?
                WHERE channel_id = ?
                AND guild_id = ?
                """,
                (timestamp, channel_id, guild_id),
            )
            await db.commit()

    async def iterate_voice_rows(self) -> AsyncGenerator[tuple, None]:
        """Snapshot rows and release the read connection before callers write."""
        async with self.connect() as db:
            async with db.execute("""
                SELECT guild_id, channel_id, lobby_id, owner_id, channel_name,
                       purpose, last_dc_time, save_settings, settings_json
                FROM voice_channels
            """) as cur:
                rows = await cur.fetchall()
        for row in rows:
            yield row

    async def delete_voice_channel(self, guild_id: int, lobby_id: int, channel_id: int) -> None:
        """
        Remove one voice-channel entry by its guild, lobby and channel IDs.
        """
        async with self.connect() as db:
            await db.execute(
                """
                DELETE
                FROM voice_channels
                WHERE guild_id = ?
                AND lobby_id = ?
                AND channel_id = ?
                """,
                (guild_id, lobby_id, channel_id),
            )
            await db.commit()

    # -------------------------------------------------------------------------------
    #  LOBBY helpers
    # -------------------------------------------------------------------------------
    async def set_lobby(
        self,
        guild_id: int,
        lobby_id: int,
        settings_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Insert a lobby for a given channel.
        The lobby_id is the existing Discord channel identifier.
        """
        settings = json.dumps(normalize_settings(settings_json or {}))
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO lobbies (guild_id, lobby_id, settings_json)
                VALUES (?, ?, ?)
                """,
                (guild_id, lobby_id, settings),
            )
            await db.commit()

    async def get_lobby(
        self, guild_id: int, lobby_id: int
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a lobby row (or None)."""
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT settings_json
                FROM lobbies
                WHERE guild_id = ? AND lobby_id = ?
                """,
                (guild_id, lobby_id),
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                settings_json = row

                return {
                    "guild_id": guild_id,
                    "lobby_id": lobby_id,
                    "settings_json": normalize_settings(json.loads(settings_json[0] or "{}")),
                }

    async def get_settings_lobby(self, guild_id: int, lobby_id: int) -> Dict[str, Any]:
        """Return validated lobby settings, or defaults when the lobby is missing."""
        async with self.connect() as db:
            async with db.execute(
                "SELECT settings_json FROM lobbies WHERE guild_id = ? AND lobby_id = ?", (guild_id, lobby_id)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return normalize_settings(json.loads(row[0]))

                return normalize_settings({})

    async def update_lobby_setting(
        self, guild_id: int, lobby_id: int, key: str, value: Any
    ) -> None:
        """Atomically set a single JSON key for a lobby."""
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE lobbies
                SET settings_json = json_set(settings_json, ?, json(?))
                WHERE guild_id = ? AND lobby_id = ?
                """,
                (f"$.{key}", json.dumps(value), guild_id, lobby_id),
            )
            await db.commit()

    async def delete_lobby(self, guild_id: int, lobby_id: int) -> None:
        """Remove a lobby entry."""
        async with self.connect() as db:
            await db.execute(
                """
                DELETE FROM lobbies
                WHERE guild_id = ? AND lobby_id = ?
                """,
                (guild_id, lobby_id),
            )
            await db.commit()

    async def get_member_voice_channels(self, guild_id: int, owner_id: int) -> list[dict]:
        """Return all temporary rooms for a member across this server's lobbies."""
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM voice_channels WHERE guild_id=? AND owner_id=?",
                (guild_id, owner_id),
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def delete_settings_user(self, guild_id: int, lobby_id: int, user_id: int) -> None:
        async with self.connect() as db:
            await db.execute("DELETE FROM users WHERE guild_id=? AND lobby_id=? AND user_id=?",
                             (guild_id, lobby_id, user_id))
            await db.commit()

    async def set_semi_room(self, guild_id: int, channel_id: int, owner_id: int,
                            role_id: int, created_role: bool) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO semi_rooms (guild_id,channel_id,owner_id,role_id,created_role) VALUES (?,?,?,?,?)",
                (guild_id, channel_id, owner_id, role_id, int(created_role)),
            )
            await db.commit()

    async def get_semi_room(self, guild_id: int, channel_id: int) -> Optional[dict]:
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM semi_rooms WHERE guild_id=? AND channel_id=?",
                                  (guild_id, channel_id)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def list_semi_rooms(self, guild_id: Optional[int] = None) -> list[dict]:
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM semi_rooms"
            params = ()
            if guild_id is not None:
                query += " WHERE guild_id=?"
                params = (guild_id,)
            async with db.execute(query, params) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def update_semi_state(self, channel_id: int, *, hidden: bool,
                                expires_at: Optional[int]) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE semi_rooms SET hidden=?,expires_at=? WHERE channel_id=?",
                             (int(hidden), expires_at, channel_id))
            await db.commit()

    async def delete_semi_room(self, guild_id: int, channel_id: int) -> None:
        async with self.connect() as db:
            await db.execute("DELETE FROM semi_rooms WHERE guild_id=? AND channel_id=?",
                             (guild_id, channel_id))
            await db.commit()

    async def list_lobbies(self, guild_id):
        async with self.connect() as db:
            async with db.execute("SELECT lobby_id, settings_json FROM lobbies WHERE guild_id=?", (guild_id,)) as cur:
                return [{"lobby_id": row[0], "settings_json": normalize_settings(json.loads(row[1]))}
                        for row in await cur.fetchall()]

    async def list_lobby_locks(self, guild_id):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM semi_lobby_locks WHERE guild_id=?", (guild_id,)) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def save_lobby_lock(self, guild_id, lobby_id, user_id, previous_connect):
        async with self.connect() as db:
            await db.execute("INSERT OR IGNORE INTO semi_lobby_locks VALUES (?,?,?,?)",
                             (guild_id, lobby_id, user_id, previous_connect))
            await db.commit()

    async def delete_lobby_lock(self, guild_id, lobby_id, user_id):
        async with self.connect() as db:
            await db.execute("DELETE FROM semi_lobby_locks WHERE guild_id=? AND lobby_id=? AND user_id=?",
                             (guild_id, lobby_id, user_id))
            await db.commit()
