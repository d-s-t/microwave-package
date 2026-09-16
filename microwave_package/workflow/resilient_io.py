"""
Retry/wait helpers for talking to the shared queue folder
(ANSYS_ANALYZE_QUEUE_PATH), which is typically a network share: if it's
temporarily unreachable (share down, VPN dropped, ...), the pipeline
should wait for it to come back rather than crash -- an interrupted run
resumes on its own once the share is reachable again.
"""
from __future__ import annotations

import inspect
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("microwave_package")


class Stopped(Exception):
    """A wait or a retry was abandoned because the run was told to stop.

    Not a failure. Nothing is lost -- whatever was waiting for the share
    will wait again when the run is resumed -- and it exists so that a
    stop can get out of a retry that would otherwise go round forever.
    A share that has stopped accepting writes is exactly the situation
    somebody presses Stop over, so a Stop that cannot interrupt this is
    not really a Stop.
    """


def _pause(seconds: float, stop_event=None) -> None:
    """Wait, unless the run is stopped -- then stop waiting, and say so."""
    if stop_event is None:
        time.sleep(seconds)
        return
    if stop_event.wait(seconds):
        raise Stopped("stopped while waiting to retry")


def path_available(path: str) -> bool:
    import os

    try:
        return os.path.isdir(path)
    except OSError:
        return False


def wait_until_available(path: str, poll_seconds: float = 10.0,
                         on_wait: Optional[Callable[[str], None]] = None,
                         stop_event=None) -> bool:
    """Blocks until `path` is reachable. Returns True if it ever had to wait.

    Raises `Stopped` if the run is stopped while waiting: a share that
    is not coming back would otherwise hold the run open forever.
    """
    waited = False
    while not path_available(path):
        if stop_event is not None and stop_event.is_set():
            raise Stopped(f"stopped while waiting for {path}")
        waited = True
        if on_wait:
            on_wait(path)
        else:
            logger.warning("Path %s is not reachable; waiting %.0fs and retrying...", path, poll_seconds)
        _pause(poll_seconds, stop_event)
    return waited


def _takes_a_stop_event(fn: Callable) -> bool:
    try:
        return "stop_event" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def call_with_retry(fn: Callable, *args, queue_root: Optional[str] = None, poll_seconds: float = 10.0,
                     on_wait: Optional[Callable[[str], None]] = None, stop_event=None, **kwargs):
    """
    Call `fn(*args, **kwargs)`, retrying forever on OSError (e.g. the
    share dropped mid-operation). If `queue_root` is given, waits for it
    to be reachable before every attempt.

    `stop_event` does two jobs, deliberately. It gets this loop out of a
    retry that would otherwise go round forever -- "retry forever" is
    right for a share that will come back and wrong for one that will
    not, and the difference is a person deciding to stop -- and it is
    handed on to `fn` when `fn` takes one, so the work itself stops at
    its own next checkpoint. A stop raises `Stopped` rather than
    returning, so no caller can mistake it for a call that went through.
    """
    if stop_event is not None and _takes_a_stop_event(fn):
        kwargs["stop_event"] = stop_event
    while True:
        if stop_event is not None and stop_event.is_set():
            raise Stopped(f"stopped before retrying {getattr(fn, '__name__', fn)}")
        if queue_root:
            wait_until_available(queue_root, poll_seconds=poll_seconds, on_wait=on_wait,
                                 stop_event=stop_event)
        try:
            return fn(*args, **kwargs)
        except OSError as exc:
            logger.warning(
                "Filesystem operation failed (%s); waiting %.0fs and retrying. "
                "Stop the run if it is not going to come back.", exc, poll_seconds,
            )
            _pause(poll_seconds, stop_event)


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
                       on_wait: Optional[Callable[[str], None]] = None,
                       stop_event=None) -> None:
    """Wait for the queue to be reachable, then create its subfolders.

    The half of `QueuePaths.from_env` that needs the share, deferred to
    the point where the share is genuinely needed.
    """
    wait_until_available(queue.root, poll_seconds=poll_seconds, on_wait=on_wait,
                         stop_event=stop_event)
    queue.ensure_exist()
