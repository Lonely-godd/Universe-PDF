import logging
import time
from asyncio import sleep, timeout

from telegram.error import BadRequest, NetworkError

LOGGER = logging.getLogger(__name__)


async def download_file(incoming, path, *, total_timeout=120):
    """Retry only reads, with a deadline shared by metadata, transfer and backoff."""
    started = time.monotonic()
    stage, attempt = "get_file", 0
    size = incoming.file_size
    timeouts = dict(read_timeout=30, connect_timeout=15, write_timeout=30, pool_timeout=10)
    try:
        async with timeout(total_timeout):
            for attempt in range(1, 4):
                try:
                    stage = "get_file"
                    # Refresh Telegram's download URL on every attempt.
                    remote = await incoming.get_file(**timeouts)
                    stage = "download"
                    await remote.download_to_drive(custom_path=path, **timeouts)
                    LOGGER.info(
                        "Загрузка завершена: type=%s bytes=%s attempt=%s elapsed=%.1fs",
                        path.suffix,
                        path.stat().st_size,
                        attempt,
                        time.monotonic() - started,
                    )
                    return
                except BadRequest:
                    # BadRequest also inherits NetworkError, but retry cannot fix invalid files.
                    raise
                except NetworkError as error:
                    path.unlink(missing_ok=True)
                    if attempt == 3:
                        raise
                    LOGGER.warning(
                        "Повтор загрузки: stage=%s type=%s size=%s attempt=%s "
                        "error=%s cause=%s elapsed=%.1fs",
                        stage,
                        path.suffix,
                        size,
                        attempt,
                        type(error).__name__,
                        type(error.__cause__).__name__,
                        time.monotonic() - started,
                    )
                    await sleep(attempt)
    except BaseException as error:
        path.unlink(missing_ok=True)
        # Exception text/tracebacks may contain the download URL with the bot token.
        LOGGER.warning(
            "Загрузка прервана: stage=%s type=%s size=%s attempt=%s "
            "error=%s cause=%s elapsed=%.1fs",
            stage,
            path.suffix,
            size,
            attempt,
            type(error).__name__,
            type(error.__cause__).__name__,
            time.monotonic() - started,
        )
        raise
