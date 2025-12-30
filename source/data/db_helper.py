# db_helper.py
import aiosqlite
import json
import time
from typing import Optional, Dict, Any

class VoiceDB:
    def __init__(self, path: str = "utility_bot.db"):
        self.path = path

    async def init(self):
        """Create tables if they don't exist."""
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS voice_channels (
                    channel_id      INTEGER PRIMARY KEY,
                    guild_id        INTEGER NOT NULL,
                    owner_id        INTEGER NOT NULL,
                    purpose         TEXT,
                    game_name       TEXT,
                    private         BOOLEAN NOT NULL DEFAULT 0,
                    expires_at      INTEGER,
                    settings_json   TEXT
                );
                """
            )
            await db.commit()

    21

    async def get_channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT * FROM voice_channels WHERE channel_id = ?", (channel_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                data = dict(zip(cols, row))
                data["settings"] = json.loads(data.pop("settings_json") or "{}")
                return data

    async def delete_expired(self):
        """Remove rows whose expires_at < now."""
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM voice_channels WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            await db.commit()