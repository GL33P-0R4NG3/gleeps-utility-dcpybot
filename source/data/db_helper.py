# db_helper.py
import aiosqlite
import json
import time
from typing import Optional, Dict, Any, List


class DBHelper:
    """Async wrapper around the SQLite DB used by the bot."""

    def __init__(self, path: str = "utility_bot.db"):
        self.path = path

    # -------------------------------------------------------------------------------
    #  Initialize the database – creates all if not present
    # -------------------------------------------------------------------------------
    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS guilds (
                    guild_id      INTEGER PRIMARY KEY,
                    settings_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS users (
                    uid           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       INTEGER NOT NULL,
                    guild_id      INTEGER NOT NULL,
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE (user_id, guild_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS voice_channels (
                    vc_id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id      INTEGER NOT NULL,
                    guild_id        INTEGER NOT NULL,
                    owner_id        INTEGER NOT NULL,
                    private         BOOL NOT NULL DEFAULT FALSE,
                    purpose         TEXT,
                    channel_name    TEXT NOT NULL DEFAULT 'general',
                    last_dc_time    INTEGER,
                    settings_json   TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS lobbies (
                    lobby_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id       INTEGER NOT NULL,
                    channel_id     INTEGER NOT NULL,
                    settings_json  TEXT NOT NULL DEFAULT '{}',
                    UNIQUE (guild_id, channel_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(guild_id) ON DELETE CASCADE
                );
                """
            )
            await db.commit()

    # -------------------------------------------------------------------------------
    #  GUILD helpers
    # -------------------------------------------------------------------------------
    async def get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        """Return the JSON dict stored in guilds.settings_json. Creates row if missing."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT settings_json FROM guilds WHERE guild_id = ?", (guild_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return json.loads(row[0])

                # No row yet – create a default one
                await db.execute("INSERT INTO guilds (guild_id) VALUES (?)", (guild_id,))
                await db.commit()
                return {}

    async def set_guild_settings(self, guild_id: int, settings: Dict[str, Any]) -> None:
        """
        Replace the whole ``settings_json`` for a guild with the supplied dict.
        """
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE guilds
                SET settings_json = ?
                WHERE guild_id = ?
                """,
                (json.dumps(settings), guild_id),
            )
            await db.commit()

    # -------------------------------------------------------------------------------
    #  USER helpers (note: uid is auto‑generated, we work with user_id and guild_id)
    # -------------------------------------------------------------------------------
    async def get_user_settings(self, guild_id: int, user_id: int) -> None | Dict[str, Any]:
        """Fetch settings_json for a (user_id, guild_id) pair. Returns None if missing."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                """
                SELECT settings_json FROM users
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return json.loads(row[0])

                return None

    async def set_user_settings(
            self, guild_id: int, user_id: int, settings: Dict[str, Any]
    ) -> None:
        """
        Replace the whole ``settings_json`` for a user‑in‑guild row.
        If the row does not exist, yet it will be created.
        """
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, guild_id, settings_json)
                VALUES (?, ?, ?) ON CONFLICT(user_id, guild_id) DO
                UPDATE
                    SET settings_json = excluded.settings_json
                """,
                (user_id, guild_id, json.dumps(settings)),
            )
            await db.commit()

    # -------------------------------------------------------------------------------
    #  VOICE‑CHANNEL helpers
    # -------------------------------------------------------------------------------
    async def add_voice_channel(
        self,
        channel_id: int,
        guild_id: int,
        owner_id: int,
        purpose: Optional[str] = None,
        channel_name: str = "general",
        private: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Insert a new temporary voice‑channel row.
        Returns the autogenerated ``vc_id`` (the primary key of the table).
        """
        settings = json.dumps(extra or {})

        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO voice_channels
                (channel_id, guild_id, owner_id, private, purpose,
                 channel_name, settings_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    guild_id,
                    owner_id,
                    int(private),
                    purpose,
                    channel_name,
                    settings,
                ),
            )
            await db.commit()
            return cursor.lastrowid  # this is the vc_id

    async def get_voice_channel(self, guild_id: int, channel_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a voice‑channel row by its Discord ``channel_id``."""
        async with aiosqlite.connect(self.path) as db:
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
                data["settings"] = json.loads(data.pop("settings_json") or "{}")
                return data

    async def set_voice_channel_settings(
        self, channel_id: int, settings: Dict[str, Any]
    ) -> None:
        """
        Replace the whole ``settings_json`` for a temporary voice channel.
        """
        async with aiosqlite.connect(self.path) as db:
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
        If ``timestamp`` is omitted the current epoch seconds are used.
        """
        ts = timestamp if timestamp is not None else int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE voice_channels
                SET last_dc_time = ?
                WHERE channel_id = ?
                AND guild_id = ?
                """,
                (ts, channel_id, guild_id),
            )
            await db.commit()

    # -------------------------------------------------------------------------------
    #  Delete a **single** voice‑channel row (by guild_id + channel_id)
    # -------------------------------------------------------------------------------
    async def delete_voice_channel(self, guild_id: int, channel_id: int) -> None:
        """
        Remove ONE voice‑channel entry identified by the (guild_id, channel_id)
        pair.
        """
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                DELETE
                FROM voice_channels
                WHERE guild_id = ?
                AND channel_id = ?
                """,
                (guild_id, channel_id),
            )
            await db.commit()

    # -------------------------------------------------------------------------------
    #  LOBBY helpers (unchanged except for naming consistency)
    # -------------------------------------------------------------------------------
    async def add_lobby(
        self,
        guild_id: int,
        channel_id: int,
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Insert (or replace) a lobby for a given channel.
        Returns the autogenerated ``lobby_id``.
        """
        settings = json.dumps(extra or {})
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO lobbies (guild_id, channel_id, settings_json)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, channel_id) DO UPDATE
                SET settings_json = excluded.settings_json
                """,
                (guild_id, channel_id, settings),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_lobby(
        self, guild_id: int, channel_id: int
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a lobby row (or None)."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                """
                SELECT lobby_id, settings_json
                FROM lobbies
                WHERE guild_id = ? AND channel_id = ?
                """,
                (guild_id, channel_id),
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                lobby_id, settings_json = row
                return {
                    "lobby_id": lobby_id,
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "settings": json.loads(settings_json or "{}"),
                }

    async def update_lobby_setting(
        self, guild_id: int, channel_id: int, key: str, value: Any
    ) -> None:
        """Atomically set a single JSON key for a lobby."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE lobbies
                SET settings_json = json_set(settings_json, ?, ?)
                WHERE guild_id = ? AND channel_id = ?
                """,
                (f"$.{key}", json.dumps(value), guild_id, channel_id),
            )
            await db.commit()

    async def delete_lobby(self, guild_id: int, channel_id: int) -> None:
        """Remove a lobby entry."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                DELETE FROM lobbies
                WHERE guild_id = ? AND channel_id = ?
                """,
                (guild_id, channel_id),
            )
            await db.commit()
