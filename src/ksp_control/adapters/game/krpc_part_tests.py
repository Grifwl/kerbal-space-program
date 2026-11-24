"""kRPC adapter for the part test assistant.

Notes on kRPC 0.8.0 that shape this adapter:

* Contract objectives are only available as display text; the texts are
  parsed once per contract (``contract_text``) and cached, while the
  completion flags are re-read on every call because the game updates
  them each frame.
* ``ContractParameter.notes`` raises when the game has no notes (it
  returns null although the procedure is declared non-nullable).
* Part events are reached through ``Module.event_list``; the test event of
  ``ModuleTestSubject`` is named ``RunTestEvent`` in every language.
* ``UI.message`` takes the colour as a ``ColorRGBA`` tuple.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ksp_control.adapters.game.contract_text import PART_TEST_CONTRACT_TYPE, parse_requirement
from ksp_control.adapters.game.krpc_connection import (
    RECOVERABLE_ERRORS,
    KrpcEndpoint,
    connect,
    is_connection_problem,
    read_scene,
)
from ksp_control.application.ports import (
    GameUnavailableError,
    Notice,
    NoticeLevel,
    NoticePlacement,
    PartTestGamePort,
)
from ksp_control.domain.model import GameScene
from ksp_control.domain.part_tests import (
    EditorState,
    FlightState,
    PartTestRequirement,
    RequirementProgress,
    Situation,
    TestContract,
    TestPart,
)

logger = logging.getLogger(__name__)

TEST_MODULE = "ModuleTestSubject"
TEST_EVENT = "RunTestEvent"

# Message colours (RGBA) per meaning.
_COLOURS = {
    NoticeLevel.SUCCESS: (0.2, 1.0, 0.3, 1.0),
    NoticeLevel.ACTION: (1.0, 0.9, 0.1, 1.0),
    NoticeLevel.INFO: (0.9, 0.95, 1.0, 1.0),
    NoticeLevel.WARNING: (1.0, 0.55, 0.15, 1.0),
    NoticeLevel.ERROR: (1.0, 0.25, 0.2, 1.0),
}

# Font sizes per placement: banners must be noticed, side notes must not
# get in the way.
_SIZES = {NoticePlacement.BANNER: 30.0, NoticePlacement.SIDE: 18.0}

# How long the list of test parts of a vessel is reused: parts only change
# when staging, docking or breaking up.
_PARTS_TTL = 3.0


class _CachedContract:
    """Remote objects and parsed terms of one active contract.

    Attributes:
        remote: The remote ``Contract``.
        objective: Its "Test <part>" objective.
        conditions: The objective's sub-objectives.
        requirement: The parsed requirement.
    """

    def __init__(self, remote: Any, objective: Any, conditions: list[Any],
                 requirement: PartTestRequirement) -> None:
        """Store the cached values.

        Args:
            remote: The remote ``Contract``.
            objective: Its main objective.
            conditions: The sub-objectives.
            requirement: The parsed requirement.
        """
        self.remote = remote
        self.objective = objective
        self.conditions = conditions
        self.requirement = requirement


class KrpcPartTestGame(PartTestGamePort):
    """Reads part test contracts and the active vessel; acts on the vessel.

    Responsibilities: parse and cache contracts, read flight telemetry,
    set the throttle, press "Run Test" and show on-screen messages.

    Usage notes: not thread safe; one instance per polling loop. Every
    kRPC failure is turned into ``GameUnavailableError`` and the connection
    is reopened on the next call when the socket broke.
    """

    def __init__(self, endpoint: KrpcEndpoint, client_name: str = "KSP part tests") -> None:
        """Configure the adapter; the connection is opened on first use.

        Args:
            endpoint: kRPC server location.
            client_name: Name shown in the kRPC server window in KSP.
        """
        self._endpoint = endpoint
        self._client_name = client_name
        self._conn: Any = None
        self._contracts: dict[Any, _CachedContract | None] = {}
        self._by_id: dict[str, _CachedContract] = {}
        self._parts: tuple[Any, float, tuple[TestPart, ...] | None] | None = None

    # ---------------------------------------------------------------- contracts

    def read_test_contracts(self) -> list[TestContract]:
        """Read the active part test contracts and their live progress.

        Returns:
            The contracts; empty outside a career game.
        """
        def read(conn: Any) -> list[TestContract]:
            try:
                active = conn.space_center.contract_manager.active_contracts
            except RECOVERABLE_ERRORS as error:
                if is_connection_problem(error):
                    raise
                return []  # not a career game, or a scene still loading
            result = []
            for remote in active:
                cached = self._cached(remote)
                if cached is None:
                    continue
                flags = [bool(c.completed) for c in cached.conditions]
                result.append(TestContract(
                    cached.requirement,
                    RequirementProgress(
                        done=bool(cached.objective.completed),
                        conditions_met=all(flags) if flags else None,
                    ),
                ))
            return result

        return self._call(read)

    def contract_state(self, contract_id: str) -> str | None:
        """Tell what happened to a contract that is no longer active.

        Args:
            contract_id: Identifier from ``read_test_contracts``.

        Returns:
            The contract state name, or ``None`` when unknown.
        """
        cached = self._by_id.get(contract_id)
        if cached is None:
            return None
        try:
            state = self._call(lambda conn: cached.remote.state)
        except GameUnavailableError:
            return None
        return getattr(state, "name", str(state)).rsplit(".", 1)[-1].lower()

    def _cached(self, remote: Any) -> _CachedContract | None:
        """Parse a contract once; remember non part test contracts as None.

        Args:
            remote: Remote ``Contract`` (hashable by server object id).

        Returns:
            The cached contract, or ``None`` when it is not a part test.
        """
        if remote in self._contracts:
            return self._contracts[remote]
        cached = None
        if str(remote.type) == PART_TEST_CONTRACT_TYPE:
            objectives = list(remote.parameters)
            if objectives:
                objective = objectives[0]
                conditions = list(objective.children)
                contract_id = str(getattr(remote, "_object_id", id(remote)))
                requirement = parse_requirement(
                    contract_id,
                    str(remote.title),
                    str(objective.title),
                    _notes(objective),
                    [str(c.title) for c in conditions],
                )
                if requirement is not None:
                    cached = _CachedContract(remote, objective, conditions, requirement)
                    self._by_id[contract_id] = cached
                    logger.debug("Parsed contract %r as %s", requirement.contract_title, requirement)
                else:
                    logger.warning("Could not understand part test objective %r", objective.title)
        self._contracts[remote] = cached
        return cached

    # ---------------------------------------------------------------- flight

    def read_flight(self) -> FlightState | None:
        """Read the active vessel.

        Returns:
            Its state, or ``None`` outside the flight scene.
        """
        def read(conn: Any) -> FlightState | None:
            if read_scene(conn) is not GameScene.FLIGHT:
                return None
            space_center = conn.space_center
            vessel = space_center.active_vessel
            if vessel is None:
                return None
            body = vessel.orbit.body
            # A flight in the body's rotating frame gives surface-relative
            # speeds, the ones KSP uses for the speed windows.
            flight = vessel.flight(body.reference_frame)
            control = vessel.control
            return FlightState(
                vessel=str(vessel.name),
                body=str(body.name),
                situation=Situation.from_name(str(vessel.situation)),
                altitude=float(flight.mean_altitude),
                vertical_speed=float(flight.vertical_speed),
                surface_speed=float(flight.speed),
                orbital_speed=float(vessel.orbit.speed),
                throttle=float(control.throttle),
                thrust=float(vessel.thrust),
                available_thrust=float(vessel.available_thrust),
                mass=float(vessel.mass),
                parts=self._test_parts(vessel),
            )

        return self._call(read)

    def _test_parts(self, vessel: Any) -> tuple[TestPart, ...] | None:
        """List the vessel's test subject parts, cached for a few seconds.

        Args:
            vessel: Remote active vessel.

        Returns:
            The parts, or ``None`` when they cannot be read.
        """
        now = time.monotonic()
        if self._parts is not None and self._parts[0] == vessel and now - self._parts[1] < _PARTS_TTL:
            return self._parts[2]
        try:
            parts = tuple(_test_part(part) for part in vessel.parts.with_module(TEST_MODULE))
        except RECOVERABLE_ERRORS as error:
            if is_connection_problem(error):
                raise
            parts = None
        self._parts = (vessel, now, parts)
        return parts

    def read_editor(self) -> EditorState | None:
        """Read the craft being designed.

        Returns:
            The craft, or ``None`` outside the VAB and the SPH. Its parts
            are ``None`` when they cannot be read (an empty craft).
        """
        def read(conn: Any) -> EditorState | None:
            if not read_scene(conn).is_editor:
                return None
            vessel = conn.space_center.editor.vessel
            try:
                # Only titles matter here: test events do not exist in the
                # editor.
                parts = tuple(TestPart(title=str(part.title), stage=int(part.stage))
                              for part in vessel.parts.with_module(TEST_MODULE))
                name = str(vessel.name)
            except RECOVERABLE_ERRORS as error:
                if is_connection_problem(error):
                    raise
                return EditorState(craft="", parts=None)
            return EditorState(craft=name, parts=parts)

        return self._call(read)

    def set_throttle(self, value: float) -> None:
        """Set the active vessel's main throttle.

        Args:
            value: Between 0 and 1.
        """
        def write(conn: Any) -> None:
            vessel = conn.space_center.active_vessel
            if vessel is not None:
                vessel.control.throttle = float(value)

        self._call(write)

    def run_test(self, part_title: str) -> bool:
        """Press "Run Test" on a part of the active vessel.

        Args:
            part_title: Title of the part.

        Returns:
            True when a part offered the action and it was triggered.
        """
        def press(conn: Any) -> bool:
            vessel = conn.space_center.active_vessel
            if vessel is None:
                return False
            wanted = " ".join(part_title.split()).casefold()
            for part in vessel.parts.with_module(TEST_MODULE):
                if " ".join(str(part.title).split()).casefold() != wanted:
                    continue
                event = _test_event(part)
                if event is not None and event.active:
                    event.trigger()
                    return True
            return False

        return self._call(press)

    # ---------------------------------------------------------------- screen

    def show(self, notice: Notice) -> None:
        """Show a message on the game screen.

        Args:
            notice: The message.
        """
        def display(conn: Any) -> None:
            positions = conn.ui.MessagePosition
            position = (positions.top_center if notice.placement is NoticePlacement.BANNER
                        else positions.top_right)
            conn.ui.message(notice.text, notice.duration, position,
                            _COLOURS[notice.level], _SIZES[notice.placement])

        try:
            self._call(display)
        except GameUnavailableError as error:
            # A message that cannot be shown is not worth stopping for.
            logger.debug("Message not shown: %s", error)

    def close(self) -> None:
        """Close the kRPC connection, if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing must never fail
                pass
        self._conn = None
        self._contracts.clear()
        self._by_id.clear()
        self._parts = None

    def _call(self, action: Any) -> Any:
        """Run an action against the game, translating kRPC errors.

        Args:
            action: Callable receiving the kRPC client.

        Returns:
            Whatever the action returns.

        Raises:
            GameUnavailableError: On any kRPC failure.
        """
        if self._conn is None:
            self._conn = connect(self._endpoint, self._client_name)
        try:
            return action(self._conn)
        except RECOVERABLE_ERRORS as error:
            if is_connection_problem(error):
                self.close()
            # kRPC appends the C# stack trace; the first line is enough.
            raise GameUnavailableError(str(error).splitlines()[0] if str(error) else repr(error)) from error


def _notes(parameter: Any) -> str | None:
    """Read an objective's notes, which may be missing.

    Args:
        parameter: Remote ``ContractParameter``.

    Returns:
        The notes, or ``None``.
    """
    try:
        return str(parameter.notes)
    except RECOVERABLE_ERRORS as error:
        if is_connection_problem(error):
            raise
        return None


def _test_event(part: Any) -> Any:
    """Find the "Run Test" event of a part.

    Args:
        part: Remote ``Part`` with a ``ModuleTestSubject``.

    Returns:
        The remote ``PartEvent``, or ``None``.
    """
    for module in part.modules:
        if module.name == TEST_MODULE:
            return next((e for e in module.event_list if e.name == TEST_EVENT), None)
    return None


def _test_part(part: Any) -> TestPart:
    """Describe a test subject part.

    Args:
        part: Remote ``Part``.

    Returns:
        Title, activation stage and whether "Run Test" is available.
    """
    event = _test_event(part)
    return TestPart(
        title=str(part.title),
        stage=int(part.stage),
        can_run_test=bool(event is not None and event.active),
    )

