import asyncio
import logging
import sys

from config import validate_config
from database import init_db
from notifiers import build_notifiers
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

    notifiers = build_notifiers()

    # Single long-running task — poll_loop never returns
    await poll_loop(notifiers)


if __name__ == "__main__":
    asyncio.run(main())
