"""Animated terminal spinner for long-running operations."""

import sys
import threading

from turnstone.ui.colors import DIM, RESET


class Spinner:
    """Braille-character animated spinner for terminal display."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, message: str = "Thinking"):
        self.message = message
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        i = 0
        while not self._stop_event.wait(0.08):
            frame = self._FRAMES[i % len(self._FRAMES)]
            sys.stderr.write(f"\r{DIM}{frame} {self.message}…{RESET}  ")
            sys.stderr.flush()
            i += 1

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join()
            self._thread = None
        sys.stderr.write("\r\033[2K")
        sys.stderr.flush()

    def __enter__(self) -> "Spinner":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
