import threading


class CancellationToken:
    """Lightweight cancellation/pause signal passed into ETL workers.

    Controllers call ``is_cancelled()`` or ``is_paused()`` at the start of
    each batch to honour stop/pause requests without touching in-flight work.
    """

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._pause = threading.Event()

    # ------------------------------------------------------------------
    # Control API (called by MigrationManager)
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    # ------------------------------------------------------------------
    # Query API (called by batch workers)
    # ------------------------------------------------------------------

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def is_paused(self) -> bool:
        return self._pause.is_set()

    def wait_if_paused(self, poll_interval: float = 0.5) -> bool:
        """Block while paused; return False immediately if cancelled."""
        while self._pause.is_set():
            if self._cancel.is_set():
                return False
            self._pause.wait(timeout=poll_interval)
        return not self._cancel.is_set()
