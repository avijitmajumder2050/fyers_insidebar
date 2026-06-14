"""
s3_log_handler.py — Non-blocking S3 log handler.

Buffers log records in memory and flushes to S3 on a background
thread every FLUSH_INTERVAL_SEC seconds, and once more on process exit.

The log is APPENDED to S3 by:
  1. Reading the existing object (if any)
  2. Appending the new buffer
  3. Writing the combined content back

S3 key: trading-bot/logs/fyers_insidebar.log
Bucket: dhan-trading-data

Usage (call once at startup, before any logging):
    from s3_log_handler import setup_logging
    setup_logging()
"""

import atexit
import io
import logging
import queue
import threading
import time

import boto3

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────
S3_BUCKET          = "dhan-trading-data"
S3_LOG_KEY         = "trading-bot/logs/fyers_insidebar.log"
AWS_REGION         = "ap-south-1"
FLUSH_INTERVAL_SEC = 60        # background flush every 60 s
LOG_FORMAT         = "%(asctime)s IST [%(levelname)-8s] %(name)s — %(message)s"
LOG_DATE_FORMAT    = "%Y-%m-%d %H:%M:%S"

# IST = UTC+5:30
import datetime as _dt
_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


# ─────────────────────────────────────────────────────────────
# S3 log handler
# ─────────────────────────────────────────────────────────────

class _ISTFormatter(logging.Formatter):
    """Logging Formatter that stamps records in IST (UTC+5:30)."""
    def converter(self, timestamp):
        return _dt.datetime.fromtimestamp(timestamp, tz=_IST).timetuple()


class S3LogHandler(logging.Handler):
    """
    Thread-safe logging.Handler that accumulates formatted log lines
    in a queue and flushes them to S3 asynchronously.

    Writes are non-blocking from the caller's perspective — records
    are enqueued and the background thread does the actual S3 I/O.
    """

    def __init__(self, bucket: str, key: str, region: str, flush_interval: int) -> None:
        super().__init__()
        self._bucket   = bucket
        self._key      = key
        self._s3       = boto3.client("s3", region_name=region)
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop_evt = threading.Event()

        # Background flush thread
        self._thread = threading.Thread(
            target=self._flush_loop,
            args=(flush_interval,),
            name="s3-log-flusher",
            daemon=True,
        )
        self._thread.start()

        # Guaranteed final flush on clean exit
        atexit.register(self._final_flush)

    # ── logging.Handler interface ─────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        """Called by the logging framework — just enqueue, never block."""
        try:
            line = self.format(record) + "\n"
            self._queue.put_nowait(line)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self._stop_evt.set()
        super().close()

    # ── Internal flush mechanics ──────────────────────────────

    def _drain(self) -> str:
        """Drain all currently queued lines into a single string."""
        lines = []
        while True:
            try:
                lines.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return "".join(lines)

    def _append_to_s3(self, new_content: str) -> None:
        """Read existing S3 object (if any), append new_content, write back."""
        existing = ""
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=self._key)
            existing = obj["Body"].read().decode("utf-8")
        except self._s3.exceptions.NoSuchKey:
            pass   # first write — no existing log
        except Exception as exc:
            # Log to stderr only (avoid recursive logging)
            import sys
            print(f"[s3_log_handler] read error: {exc}", file=sys.stderr)

        combined = existing + new_content
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=self._key,
                Body=combined.encode("utf-8"),
                ContentType="text/plain",
            )
        except Exception as exc:
            import sys
            print(f"[s3_log_handler] write error: {exc}", file=sys.stderr)

    def _flush_loop(self, interval: int) -> None:
        """Background thread: flush every `interval` seconds."""
        while not self._stop_evt.wait(timeout=interval):
            content = self._drain()
            if content:
                self._append_to_s3(content)

    def _final_flush(self) -> None:
        """Called by atexit — flush whatever remains in the queue."""
        self._stop_evt.set()
        content = self._drain()
        if content:
            self._append_to_s3(content)


# ─────────────────────────────────────────────────────────────
# Public setup function
# ─────────────────────────────────────────────────────────────

def setup_logging(level: int = logging.INFO) -> None:
    """
    Configure root logger with:
      • StreamHandler  → stdout (visible in Docker logs / CloudWatch)
      • S3LogHandler   → s3://dhan-trading-data/trading-bot/logs/fyers_insidebar.log

    Call this ONCE at the very start of strategy_engine.py __main__,
    before any other imports that call logging.getLogger().
    """
    formatter = _ISTFormatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # ── Console handler (Docker stdout) ──────────────────────
    console = logging.StreamHandler()
    console.setFormatter(formatter)

    # ── S3 handler ────────────────────────────────────────────
    s3_handler = S3LogHandler(
        bucket=S3_BUCKET,
        key=S3_LOG_KEY,
        region=AWS_REGION,
        flush_interval=FLUSH_INTERVAL_SEC,
    )
    s3_handler.setFormatter(formatter)

    # ── Root logger ───────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(s3_handler)

    # Suppress noisy boto3 / urllib3 DEBUG noise
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("s3transfer").setLevel(logging.WARNING)

    root.info(
        "Logging initialised — console + S3 (s3://%s/%s, flush every %ds)",
        S3_BUCKET, S3_LOG_KEY, FLUSH_INTERVAL_SEC,
    )