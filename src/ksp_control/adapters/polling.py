"""Driving adapter that triggers the "record" use case periodically."""

from __future__ import annotations

import logging
import threading

from ksp_control.application.economy_monitor import EconomyMonitorService

logger = logging.getLogger(__name__)


class PollingLoop:
    """Calls ``EconomyMonitorService.poll_once`` on a background thread.

    Responsibilities: pacing (a short interval while connected, a longer one
    while the game is unreachable so we do not hammer a closed port) and a
    clean shutdown.

    Usage notes: the thread is a daemon, so it never keeps the process alive
    on its own; call ``stop`` for an orderly shutdown.
    """

    def __init__(
        self,
        service: EconomyMonitorService,
        interval: float = 2.0,
        retry_interval: float = 5.0,
    ) -> None:
        """Configure the loop.

        Args:
            service: Service whose ``poll_once`` is called.
            interval: Seconds between polls while the game is reachable.
            retry_interval: Seconds between attempts while it is not.
        """
        self._service = service
        self._interval = interval
        self._retry_interval = retry_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="economy-poller", daemon=True)

    def start(self) -> None:
        """Start polling in the background."""
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the loop to stop and wait for it.

        Args:
            timeout: Maximum seconds to wait for the thread.
        """
        self._stop.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        """Thread body: poll until stopped, never dying on an exception."""
        was_connected = None
        while not self._stop.is_set():
            try:
                self._service.poll_once()
            except Exception:  # noqa: BLE001 - a bug must not kill the monitor
                logger.exception("Unexpected error while polling the game")
            status = self._service.status
            if status.connected != was_connected:
                logger.info(status.message)
                was_connected = status.connected
            self._stop.wait(self._interval if status.connected else self._retry_interval)
