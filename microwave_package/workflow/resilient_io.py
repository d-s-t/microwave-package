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

logger = logging.getLogger("microwave_package")


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


def queue_from_env(env_var: str = "ANSYS_ANALYZE_QUEUE_PATH"):
    """`QueuePaths` for the configured queue, WITHOUT touching it.

    `QueuePaths.from_env` creates the queue's subfolders as it goes,
    which raises the moment the share is unreachable -- and it is called
    while a run is being started, so an offline share used to mean no run
    could even be created. Preparing a run offline is a normal thing to
    want: the geometry is built on this machine and only the handoff
    needs the network.

    The constructor itself is pure string joining, so this is safe to
    call with the share down. `ensure_queue_ready` does the part that
    needs the share, when the share is actually there.
    """
    import os

    from ansys_analyze_common import queue_common

    root = os.environ.get(env_var)
    if not root:
        raise EnvironmentError(
            f"Environment variable {env_var} is not set. Point it at a "
            "folder both the modeling machine and the analysis machine "
            "can reach (e.g. a shared/network drive) that should hold "
            "the task queue."
        )
    return queue_common.QueuePaths(root)


def ensure_queue_ready(queue, poll_seconds: float = 10.0,
                       on_wait: Optional[Callable[[str], None]] = None) -> None:
    """Wait for the queue to be reachable, then create its subfolders.

    The half of `QueuePaths.from_env` that needs the share, deferred to
    the point where the share is genuinely needed.
    """
    wait_until_available(queue.root, poll_seconds=poll_seconds, on_wait=on_wait)
    queue.ensure_exist()
