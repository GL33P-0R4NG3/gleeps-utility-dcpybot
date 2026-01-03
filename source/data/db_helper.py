# db_helper.py
import aiosqlite
import json
import time
from typing import Optional, Dict, Any, AsyncGenerator, Tuple


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
    async def get_settings_guild(self, guild_id: int) -> Dict[str, Any]:
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

    async def set_settings_guild(self, guild_id: int, settings: Dict[str, Any]) -> None:
        """
        Replace the whole ``settings_json`` for a guild with the supplied dict.
        """
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO guilds (guild_id, settings_json)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE
                SET settings_json = excluded.settings_json
                """,
                (guild_id, json.dumps(settings)),
            )
            await db.commit()

    # -------------------------------------------------------------------------------
    #  USER helpers (note: uid is auto‑generated, we work with user_id and guild_id)
    # -------------------------------------------------------------------------------
    async def get_settings_user(self, guild_id: int, user_id: int) -> None | Dict[str, Any]:
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

    async def set_settings_user(
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
    async def set_voice_channel(
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
                data["settings_json"] = json.loads(data.pop("settings_json") or "{}")
                return data

    async def get_count_voice_channel_by_member(self, guild_id: int, member_id: int) -> int:
        """Fetch a voice‑channel row by its Discord ``channel_id``."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                """
                SELECT COUNT(vc_id) FROM voice_channels
                WHERE owner_id = ?
                AND guild_id = ?
                """,
                (member_id, guild_id),
            ) as cur:
                row = await cur.fetchone()
                return row[0]

    async def get_count_voice_channels(self, guild_id: int) -> int:
        """
        :param guild_id:
        :return: The number of active voice channels in given guild.
        """
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                """
                SELECT COUNT(vc_id) FROM voice_channels
                WHERE guild_id = ?
                """,
                (guild_id,)
            ) as cur:
                row = await cur.fetchone()
                return row[0]

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
        async with aiosqlite.connect(self.path) as db:
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

    async def iterate_voice_rows(self) -> AsyncGenerator[Tuple[int, int, int, Dict[str, Any]], None]:
        """
        Async generator that yields (guild_id, channel_id, last_dc_time, settings_json) for every voice‑channel.

        The generator opens a cursor **once** and streams rows one at a time,
        so the DB can be updated while we are iterating.
        """
        async with aiosqlite.connect(self.path) as conn:
            # Use a *forward‑only* cursor – we never need random access.
            async with conn.execute(
                    """
                    SELECT guild_id, channel_id, last_dc_time, settings_json
                    FROM voice_channels
                    """,
                    (),
            ) as cur:
                async for row in cur:  # yields each row as a tuple
                    yield row  # (guild_id, channel_id, last_dc_time, settings_json)

    async def check_voice_expiration(self) -> Dict[str, Any]:
        """
        Checks expirations of ALL voice channels on every guild this bot is on
        and deletes all expired voice channels.

        :return: Dict with KV pair of deleted guilds (K) and channels (V).
        """

    async def delete_voice_channel(self, guild_id: int, channel_id: int) -> None:
        """
        Remove ONE voice‑channel entry identified by the (guild_id, channel_id)
        pair.
        """
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
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
    async def set_lobby(
        self,
        guild_id: int,
        channel_id: int,
        settings_json: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Insert a lobby for a given channel.
        Returns the autogenerated ``lobby_id``.
        """
        settings = json.dumps(settings_json or {})
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO lobbies (guild_id, channel_id, settings_json)
                VALUES (?, ?, ?)
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
                    "settings_json": json.loads(settings_json or "{}"),
                }

    async def get_settings_lobby(self, guild_id: int, channel_id: int) -> Dict[str, Any]:
        """Return the JSON dict stored in lobbies.settings_json. Creates row if missing."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT settings_json FROM lobbies WHERE guild_id = ?", (guild_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return json.loads(row[0])

                return {}

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
