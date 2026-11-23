"""Flask application exposing the economy dashboard.

The web layer is deliberately thin: it calls the ``dashboard`` use case and
serializes the resulting read model to JSON. The page polls ``/api/dashboard``
every couple of seconds, which is simpler and more robust than websockets
for a single local viewer.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, send_from_directory

from ksp_control.application.economy_monitor import DashboardView, EconomyMonitorService
from ksp_control.domain.model import LedgerEvent, ResourceSample

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(service: EconomyMonitorService) -> Flask:
    """Build the Flask application.

    Args:
        service: The economy monitor service (already wired with adapters).

    Returns:
        A ready to run Flask app.
    """
    app = Flask(__name__, static_folder=str(_STATIC_DIR), static_url_path="/static")

    @app.get("/")
    def index() -> Any:
        """Serve the dashboard page.

        Returns:
            The HTML page.
        """
        return send_from_directory(_STATIC_DIR, "index.html")

    @app.get("/api/dashboard")
    def dashboard() -> Any:
        """Return everything the dashboard displays.

        Returns:
            JSON document described by ``serialize_dashboard``.
        """
        response = jsonify(serialize_dashboard(service.dashboard()))
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def serialize_dashboard(view: DashboardView) -> dict[str, Any]:
    """Convert the dashboard read model to JSON-friendly primitives.

    Args:
        view: The read model.

    Returns:
        A dictionary ready for ``jsonify``.
    """
    summary = view.summary
    return {
        "status": {
            "connected": view.status.connected,
            "message": view.status.message,
            "checked_at": _time(view.status.checked_at),
        },
        "current": _sample(view.current) if view.current else None,
        "baseline": _sample(view.baseline) if view.baseline else None,
        "samples": [_sample(s) for s in view.samples],
        "summary": {
            "mission_balance": summary.mission_balance,
            "contracts_net": summary.contracts_net,
            "vessels_net": summary.vessels_net,
            "launch_costs": summary.launch_costs,
            "recoveries": summary.recoveries,
            "other": summary.other,
            "total": summary.total,
            "contract_science": summary.contract_science,
            "contract_reputation": summary.contract_reputation,
            "breakdown": [
                {"key": item.key, "label": item.label, "funds": item.funds}
                for item in summary.breakdown()
            ],
            "vessels": [
                {
                    "name": v.name,
                    "launches": v.launches,
                    "cost": v.cost,
                    "recovered": v.recovered,
                    "net": v.net,
                }
                for v in summary.vessels
            ],
        },
        "events": [_event(e) for e in view.recent_events],
    }


def _time(value: datetime | None) -> int | None:
    """Convert a datetime to epoch milliseconds (what JavaScript expects).

    Args:
        value: Timezone aware datetime, or ``None``.

    Returns:
        Milliseconds since the epoch, or ``None``.
    """
    return int(value.timestamp() * 1000) if value else None


def _sample(sample: ResourceSample) -> dict[str, Any]:
    """Serialize a history point.

    Args:
        sample: The point.

    Returns:
        A compact dictionary (short keys: there can be thousands of them).
    """
    return {
        "t": _time(sample.taken_at),
        "ut": sample.universal_time,
        "scene": sample.scene.value,
        "vessel": sample.active_vessel,
        "funds": sample.resources.funds,
        "science": sample.resources.science,
        "reputation": sample.resources.reputation,
    }


def _event(event: LedgerEvent) -> dict[str, Any]:
    """Serialize a ledger event.

    Args:
        event: The event.

    Returns:
        A dictionary for the events table.
    """
    return {
        "t": _time(event.occurred_at),
        "ut": event.universal_time,
        "kind": event.kind.value,
        "description": event.description,
        "vessel": event.vessel,
        "funds": event.delta.funds,
        "science": event.delta.science,
        "reputation": event.delta.reputation,
    }
