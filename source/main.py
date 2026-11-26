"""Application entry point and standard-library logging configuration."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

from dotenv import load_dotenv


# Only metadata from the pinned discord.py voice gateway. Raw gateway frames
# can contain credentials or encryption keys and must never be traced here.
DAVE_TRACE_MESSAGES = {
    "Sending voice websocket binary frame: opcode=%s size=%d",
    "Voice websocket binary frame received: %d bytes; seq=%s op=%s",
    "Set MLS external sender",
    "MLS proposals processed",
    "MLS commit processed for transition id %d",
    "MLS welcome processed for transition id %d",
    "Executing DAVE transition id %d",
    "Preparing for DAVE epoch %d",
}


def configure_voice_trace():
    gateway = logging.getLogger("discord.gateway")
    if gateway.getEffectiveLevel() == logging.INFO:
        gateway.addFilter(lambda record: record.levelno >= logging.INFO or
                          isinstance(record.msg, str) and record.msg in DAVE_TRACE_MESSAGES)
        gateway.setLevel(logging.DEBUG)


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
    configure_voice_trace()
    from utility_bot import BOT
    BOT.run(token, log_handler=None)


if __name__ == "__main__":
    main()
