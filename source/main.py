"""Application entry point and standard-library logging configuration."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

from dotenv import load_dotenv


def main():
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set – check your .env file")
    log_dir = Path(os.getenv("BOT_LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=os.getenv("BOT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), RotatingFileHandler(
            log_dir / "utility_bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )],
    )
    from utility_bot import BOT
    BOT.run(token, log_handler=None)


if __name__ == "__main__":
    main()
