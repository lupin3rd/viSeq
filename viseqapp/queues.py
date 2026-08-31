"""Queue plumbing for viseq (REFACTOR_LATEST.md commit 3/13).

The queue OBJECTS live in ``state``; these helpers are the only writers of
``state.ui_task_queue`` / ``state.log_queue``. Worker threads call ``ui_task``
to push UI mutations to the main thread (HIGH-1: no direct dpg calls) and
``append_log``/``log_error`` for the Logs window.
"""

import time
from collections.abc import Callable
from typing import Any

import dearpygui.dearpygui as dpg

from viseqapp import state


def ui_task(fn: Callable[[], None]) -> None:
    """Run a UI mutation on the main thread via the task queue."""
    state.ui_task_queue.put(fn)


def log_error(context: str, message: str) -> None:
    t = time.strftime("%H:%M:%S")
    state.log_queue.put(f"[{t}] ERROR: {context}: {message}")


def append_log(direction: str, address: str) -> None:
    t = time.strftime("%H:%M:%S")
    log_msg = f"[{t}] {direction}: {address}"
    state.log_queue.put(log_msg)


def enqueue_set_value(tag: str, value: Any) -> None:
    """Queue a dpg.set_value(tag, value) for the main thread, if the item exists."""

    def _set():
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)

    ui_task(_set)
