"""Run tasks outside the GUI thread with safe delivery of the result."""
from __future__ import annotations

import sys
import traceback
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from shiboken6 import isValid


class _Dispatcher(QObject):
    """Lives in the GUI thread; runs the submitted callbacks here (event queue)."""

    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # a faulty callback must not take the GUI down
            traceback.print_exc(file=sys.stderr)


_dispatcher: _Dispatcher | None = None


def _get_dispatcher() -> _Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _Dispatcher()
        app = QCoreApplication.instance()
        if app is not None:
            _dispatcher.moveToThread(app.thread())
    return _dispatcher


class Worker(QRunnable):
    """Run ``fn(*args)`` and deliver the result or exception in the GUI thread."""

    def __init__(self, fn: Callable[..., Any], args: tuple = (),
                 on_result: Callable[[Any], None] | None = None,
                 on_error: Callable[[Exception], None] | None = None,
                 parent: QObject | None = None):
        super().__init__()
        self.fn, self.args = fn, args
        self.on_result, self.on_error, self.parent = on_result, on_error, parent
        self._dispatcher = _get_dispatcher()  # created in the launching thread (the GUI)

    def run(self) -> None:
        try:
            result, error = self.fn(*self.args), None
        except Exception as e:  # noqa: BLE001 - delivered to on_error
            result, error = None, e
        self._dispatcher.call.emit(lambda: self._deliver(result, error))

    def _deliver(self, result: Any, error: Exception | None) -> None:
        if self.parent is not None and not isValid(self.parent):
            return  # the recipient no longer exists
        if error is None:
            if self.on_result:
                self.on_result(result)
        elif self.on_error:
            self.on_error(error)
        else:
            print(f"pdrive: unhandled error: {error!r}", file=sys.stderr)


def run_async(fn: Callable[..., Any], *args: Any,
              on_result: Callable[[Any], None] | None = None,
              on_error: Callable[[Exception], None] | None = None,
              parent: QObject | None = None,
              pool: QThreadPool | None = None) -> Worker:
    """Launch ``fn(*args)`` in a thread; callbacks run in the GUI thread."""
    w = Worker(fn, args, on_result, on_error, parent)
    (pool or QThreadPool.globalInstance()).start(w)
    return w


def wait_idle(timeout_ms: int = 5000) -> bool:
    """Wait for the global pool workers to finish (clean shutdown)."""
    return QThreadPool.globalInstance().waitForDone(timeout_ms)
