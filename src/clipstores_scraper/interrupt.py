"""Make Ctrl-C stop the enrich run immediately.

Enrichment is resumable: every scene is marked atomically as it's written (the
marker tag rides in the same sceneUpdate as the metadata), so the next run skips
it. A hard stop therefore never loses or half-writes work -- which means there's
no reason to be graceful. And a long blocking request can occupy the main thread, so
a cooperative "stop after the current scene" can lag ~a minute and feels dead.

So: a daemon watcher thread, fed by signal.set_wakeup_fd, notices SIGINT even
while the main thread is blocked in a render, and exits the moment Ctrl-C arrives.
"""

from __future__ import annotations

import os
import select
import signal
import threading

_armed = False
_fds: tuple[int, int] | None = None  # keep the pipe alive


def _noop(_signum: int, _frame: object) -> None:
    # A real handler (not SIG_DFL/SIG_IGN) must be installed for set_wakeup_fd to
    # write the signal byte; the watcher thread does the work.
    pass


def arm() -> None:
    """Install SIGINT handling. Call once, from the main thread."""
    global _armed, _fds
    if _armed:
        return
    signal.signal(signal.SIGINT, _noop)
    r, w = os.pipe()
    os.set_blocking(r, False)
    os.set_blocking(w, False)
    signal.set_wakeup_fd(w)
    _fds = (r, w)
    threading.Thread(target=_watch, args=(r,), daemon=True).start()
    _armed = True


def _watch(r: int) -> None:
    while True:
        select.select([r], [], [], None)
        try:
            data = os.read(r, 4096)
        except BlockingIOError:
            continue
        # set_wakeup_fd writes the signal number per signal; react only to SIGINT
        # (other signals can be delivered here too). `in` on bytes tests the value.
        if signal.SIGINT in data:
            print(
                "\n⏹  Interrupted — stopping (progress saved; re-run to resume).",
                flush=True,
            )
            os._exit(130)
