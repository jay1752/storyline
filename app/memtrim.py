"""Hand freed memory back to the operating system.

Python and SQLite release memory to the C allocator (glibc malloc) when a request finishes, but the allocator keeps most of it for itself, and
with many request threads it ratchets up and stays up. Measured on the Stock page: 30 pages built and dropped left the process 67 MB bigger; 60
more through 8 threads left it 100 MB bigger; malloc_trim(0) gave back 51 MB and 60 MB of that. On the 1 GB VM (the app is capped at 640 MB)
that ratchet is what ended in the out-of-memory kill of 16 Oct 2026. So a light background thread trims every 30 seconds, and logs when the
process is large even after trimming, so a real leak would show up in the logs instead of as a crash."""
import ctypes
import logging
import threading
import time

logger = logging.getLogger(__name__)
INTERVAL = 30          # seconds between trims; each one takes a few milliseconds
WARN_MB = 450          # still this big after a trim: say so in the log (the container limit is 640)


def rss_mb() -> int | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def _loop() -> None:
    try:
        trim = ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):  # not glibc (macOS dev, musl): nothing to do
        return
    warned = 0.0
    while True:
        time.sleep(INTERVAL)
        try:
            trim(0)
            size = rss_mb()
            if size and size > WARN_MB and time.time() - warned > 600:
                warned = time.time()
                logger.warning("memory: process is %d MB after trimming (limit 640 MB)", size)
        except Exception:  # noqa: BLE001 - housekeeping must never take the app down
            logger.exception("memory trim failed")


def start() -> None:
    threading.Thread(target=_loop, name="memtrim", daemon=True).start()
