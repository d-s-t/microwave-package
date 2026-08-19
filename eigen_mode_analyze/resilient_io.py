"""
Retry/wait helpers for talking to the shared queue folder
(ANSYS_ANALYZE_QUEUE_PATH), which is typically a network share: if it's
temporarily unreachable (share down, VPN dropped, ...), the pipeline
should wait for it to come back rather than crash -- an interrupted run
resumes on its own once the share is reachable again.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("eigen_mode_analyze")


def path_available(path: str) -> bool:
    import os

    try:
        return os.path.isdir(path)
    except OSError:
        return False


def wait_until_available(path: str, poll_seconds: float = 10.0, on_wait: Optional[Callable[[str], None]] = None) -> bool:
    """Blocks until `path` is reachable. Returns True if it ever had to wait."""
    waited = False
    while not path_available(path):
        waited = True
        if on_wait:
            on_wait(path)
        else:
            logger.warning("Path %s is not reachable; waiting %.0fs and retrying...", path, poll_seconds)
        time.sleep(poll_seconds)
    return waited


def call_with_retry(fn: Callable, *args, queue_root: Optional[str] = None, poll_seconds: float = 10.0,
                     on_wait: Optional[Callable[[str], None]] = None, **kwargs):
    """
    Call `fn(*args, **kwargs)`, retrying forever on OSError (e.g. the
    share dropped mid-operation). If `queue_root` is given, waits for it
    to be reachable before every attempt.
    """
    while True:
        if queue_root:
            wait_until_available(queue_root, poll_seconds=poll_seconds, on_wait=on_wait)
        try:
            return fn(*args, **kwargs)
        except OSError as exc:
            logger.warning("Filesystem operation failed (%s); will wait and retry.", exc)
            time.sleep(poll_seconds)
