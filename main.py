import asyncio
import logging
import sys

from config import validate_config
from database import init_db
from notifiers import build_notifiers
from bot import load_state, bot_listener
from poller import poll_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("=== Augur Intel TFR Monitor starting ===")

    validate_config()
    await init_db()

    # Restore persisted interval override (set via Telegram command) or fall back to config
    state = await load_state()
    logger.info(f"Starting with poll interval: {state.poll_interval}s")

    notifiers = build_notifiers()

    # Run poll loop and Telegram bot listener concurrently
    await asyncio.gather(
        poll_loop(notifiers, state),
        bot_listener(state, notifiers),
    )


if __name__ == "__main__":
    asyncio.run(main())
