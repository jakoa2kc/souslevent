"""Background job runner for long WindNinja/OpenFOAM solves (ADR-0009).

Keeps the IHM responsive: a solve runs on a worker QThread; progress and completion come
back as Qt signals (delivered on the GUI thread). Cancellation cooperatively terminates the
WindNinja subprocess via flow.windninja's streaming runner.

The work function has signature ``fn(on_progress, cancel) -> result`` where
``on_progress(pct: int, msg: str)`` and ``cancel() -> bool``. It must NOT touch Qt widgets;
do all UI updates in the `finished`/`failed` slots.
"""

from __future__ import annotations

from PySide6 import QtCore


class _Worker(QtCore.QObject):
    progress = QtCore.Signal(int, str)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        return self._cancelled

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = self._fn(
                on_progress=lambda pct, msg: self.progress.emit(int(pct), str(msg)),
                cancel=self._is_cancelled,
            )
            self.finished.emit(result)
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SolveJob(QtCore.QObject):
    """Run ``fn(on_progress, cancel)`` on a worker thread; relay progress/result as signals.

    Signals: ``progress(int, str)``, ``finished(object)``, ``failed(str)``. ``cancelled`` is
    reported via ``failed`` after a ``cancel()`` (the message contains "cancelled").
    """

    progress = QtCore.Signal(int, str)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._thread = QtCore.QThread()
        self._worker = _Worker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._worker.cancel()

    def is_running(self) -> bool:
        return self._thread.isRunning()

    def wait(self, msecs: int | None = None) -> bool:
        if msecs is None:
            return self._thread.wait()
        return self._thread.wait(msecs)

    def shutdown(self, msecs: int = 1000) -> None:
        """Best-effort stop for app shutdown. ``wait()`` alone deadlocks when called from the GUI
        thread: ``_teardown`` (which quits the QThread) is a queued slot delivered to that same GUI
        thread — blocked in ``wait`` — so the thread never quits and ``wait`` times out every time.
        Here we disconnect the callbacks (so a late finish can't touch a dying window), quit the
        thread, and if a blocking call (e.g. an in-flight network fetch) keeps it alive, terminate
        it. Only use for read-only jobs — never for a solve that may be mid-write."""
        for sig in (self._worker.progress, self._worker.finished, self._worker.failed):
            try:
                sig.disconnect()
            except Exception:
                pass
        self.cancel()
        self._thread.quit()
        if not self._thread.wait(msecs):
            self._thread.terminate()
            self._thread.wait(500)

    @QtCore.Slot(object)
    def _on_finished(self, result) -> None:
        self._teardown()
        self.finished.emit(result)

    @QtCore.Slot(str)
    def _on_failed(self, msg) -> None:
        self._teardown()
        self.failed.emit(msg)

    def _teardown(self) -> None:
        self._thread.quit()
        self._thread.wait()
