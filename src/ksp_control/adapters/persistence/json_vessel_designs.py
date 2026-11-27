"""Registry of the craft design each renamed vessel was launched from, in JSON.

KSP keeps no trace of the craft a vessel was built from once it is
renamed, and the scripts that rename vessels (``ksp-tourism`` names them
"Cruise John Doe") run apart from the economy monitor. This small file is
where the first tell the second: ``{"Cruise John Doe": "Passatgers v1.3"}``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from ksp_control.application.ports import VesselDesignsPort

logger = logging.getLogger(__name__)


class JsonVesselDesigns(VesselDesignsPort):
    """Vessel name -> craft design, stored in a JSON file.

    Responsibilities: read the whole mapping, add one entry, and never let a
    file problem stop the caller (a missing or broken file reads as empty,
    a failed write is logged).

    Usage notes: safe across threads of one process. Two processes writing
    at the same moment could lose one entry; writes are rare (one per
    launch) and each one replaces the file atomically, so the file itself
    never ends up half written.
    """

    def __init__(self, path: Path | str) -> None:
        """Point the registry at a file (created on the first record).

        Args:
            path: The JSON file.
        """
        self._path = Path(path)
        self._lock = threading.Lock()

    def designs(self) -> dict[str, str]:
        """Read the mapping.

        Returns:
            Vessel name -> craft design; empty when the file is missing or
            unreadable.
        """
        with self._lock:
            return self._load()

    def record(self, vessel: str, design: str) -> None:
        """Remember the design of a vessel (never fails).

        Args:
            vessel: Vessel name, e.g. "Cruise John Doe".
            design: Craft it was launched from, e.g. "Passatgers v1.3".
        """
        with self._lock:
            mapping = self._load()
            mapping[vessel] = design
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # Write a sibling file and swap it in: a reader never sees
                # a half-written file.
                handle, temporary = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump(mapping, file, ensure_ascii=False, indent=1, sort_keys=True)
                os.replace(temporary, self._path)
            except OSError as error:
                logger.warning("Could not record the design of %s in %s: %s", vessel, self._path,
                               error)

    def _load(self) -> dict[str, str]:
        """Read the file without locking.

        Returns:
            The mapping, or an empty one.
        """
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning("Ignoring unreadable vessel designs file %s: %s", self._path, error)
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
