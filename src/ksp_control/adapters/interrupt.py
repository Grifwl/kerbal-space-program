"""Ctrl+C in two steps: a first press asks politely, a second one forces.

A script flying a vessel should not drop it mid-air because the player
wants it to stop: the first Ctrl+C only asks it to stop at the next safe
point (``on_first``); a second Ctrl+C raises ``KeyboardInterrupt`` as
usual, leaving everything as it is.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from types import FrameType, TracebackType
from typing import Any

logger = logging.getLogger(__name__)


class TwoStepInterrupt:
    """Context manager that turns the first Ctrl+C into a request.

    Responsibilities: install a SIGINT handler on entry, call ``on_first``
    for the first Ctrl+C, raise ``KeyboardInterrupt`` for the second, and
    restore the previous handler on exit.

    Usage notes: signal handlers can only be installed from the main
    thread. Python runs the handler between bytecodes, so a Ctrl+C pressed
    during a long blocking call to the game (a time warp, say) takes effect
    when that call returns, as with a plain ``KeyboardInterrupt``.
    """

    def __init__(self, on_first: Callable[[], None]) -> None:
        """Prepare the handler.

        Args:
            on_first: Called on the first Ctrl+C (from the signal handler:
                it must be quick and must not raise).
        """
        self._on_first = on_first
        self._presses = 0
        self._previous: Any = None

    @property
    def requested(self) -> bool:
        """Whether Ctrl+C has been pressed at least once."""
        return self._presses > 0

    def __enter__(self) -> "TwoStepInterrupt":
        """Install the handler.

        Returns:
            Itself.
        """
        self._previous = signal.signal(signal.SIGINT, self.handle)
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 traceback: TracebackType | None) -> None:
        """Restore the previous handler.

        Args:
            kind: Exception type, if any.
            error: Exception, if any.
            traceback: Its traceback, if any.
        """
        signal.signal(signal.SIGINT, self._previous)

    def handle(self, signum: int, frame: FrameType | None) -> None:
        """React to one Ctrl+C.

        Args:
            signum: The signal number.
            frame: The interrupted frame.

        Raises:
            KeyboardInterrupt: On the second press.
        """
        self._presses += 1
        if self._presses == 1:
            self._on_first()
            return
        raise KeyboardInterrupt
