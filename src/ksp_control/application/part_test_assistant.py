"""Use case: help the player complete part test contracts during a flight.

The assistant runs in the background for the whole game session. While
the player flies it:

* plans the pending tests (order, what fits in this flight, missing parts)
  and shows the plan when a flight starts;
* holds the speed inside the test's speed window with the throttle while
  climbing towards the test's altitude window;
* announces when the vessel reaches the test altitude;
* when every condition holds: runs the test itself if the part has a
  "Run Test" menu entry, or tells the player to stage if the part has to
  fire from the staging sequence; "have the part" objectives need nothing;
* announces each completed test in green.

While the player designs a craft in the VAB or the SPH, it warns (small,
red, at the side) about every pending test whose part is not on the craft.

All game access goes through ``PartTestGamePort``; time comes from an
injectable clock so the logic is testable without the game.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ksp_control.application.ports import (
    Notice,
    NoticeLevel,
    NoticePlacement,
    PartTestGamePort,
)
from ksp_control.domain.part_tests import (
    ASSISTED_BODIES,
    Activation,
    EditorState,
    FlightState,
    ObjectiveKind,
    PlanEntry,
    Situation,
    SpeedGovernor,
    TestContract,
    Verdict,
    check_conditions,
    describe_place,
    missing_parts,
    plan_tests,
    select_target,
    target_speed_range,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssistantOptions:
    """Behaviour switches.

    Attributes:
        control_throttle: Hold the speed window with the throttle.
        run_tests: Press "Run Test" automatically when conditions hold.
        repeat_seconds: How often a pending "stage now" banner is repeated.
        retry_seconds: Wait before pressing "Run Test" again if the game did
            not count the first press.
        idle_refresh_seconds: Reading period outside flight (space
            center, editors), where nothing moves fast.
        editor_reminder_seconds: How often missing parts are reminded while
            designing a craft; 0 only warns when the list changes.
        speed_aim: Where to hold the speed inside its window (0 = minimum,
            1 = maximum).
        assisted_bodies: Bodies whose tests the assistant acts on (throttle,
            Run Test, messages); tests elsewhere are left to the player.
    """

    control_throttle: bool = True
    run_tests: bool = True
    repeat_seconds: float = 3.0
    retry_seconds: float = 3.0
    idle_refresh_seconds: float = 2.0
    editor_reminder_seconds: float = 30.0
    speed_aim: float = 0.4
    assisted_bodies: tuple[str, ...] = ASSISTED_BODIES


@dataclass
class _FlightMemory:
    """What the assistant remembers during one flight (one active vessel).

    Attributes:
        vessel: Name of the vessel.
        in_altitude: Contracts whose altitude window the vessel is in.
        conditions_met: Contracts whose conditions all held last time.
        last_banner: When the last "stage now" banner was shown, per contract.
        last_run: When "Run Test" was last pressed, per contract.
        governed: Contract whose speed window the throttle is holding.
        last_command: Throttle last set by the assistant.
        released: Contracts for which the player took the throttle back.
        solid_warned: The "cannot throttle solid boosters" warning was shown.
        plan_signature: Summary of the last plan shown, to detect changes.
    """

    vessel: str
    in_altitude: set[str] = field(default_factory=set)
    conditions_met: set[str] = field(default_factory=set)
    last_banner: dict[str, float] = field(default_factory=dict)
    last_run: dict[str, float] = field(default_factory=dict)
    governed: str | None = None
    last_command: float | None = None
    released: set[str] = field(default_factory=set)
    solid_warned: bool = False
    plan_signature: tuple = ()


class PartTestAssistant:
    """Drives part test contracts from a polling loop.

    Responsibilities: call ``tick`` periodically (a few times per second
    in flight); it reads the game through the port, decides and acts.

    Usage notes: the assistant never stages for the player and only touches
    the throttle while climbing towards a speed-limited test; moving the
    throttle by hand hands control back to the player for that test.
    """

    def __init__(
        self,
        game: PartTestGamePort,
        options: AssistantOptions | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the assistant.

        Args:
            game: Access to the game.
            options: Behaviour switches.
            clock: Monotonic time source, in seconds.
        """
        self._game = game
        self._options = options or AssistantOptions()
        self._clock = clock
        self._governor = SpeedGovernor()
        self._flight: _FlightMemory | None = None
        self._contracts: dict[str, TestContract] = {}
        self._finished: set[str] = set()
        self._last_idle_read = float("-inf")
        self._idle_signature: tuple = ()
        # Editor memory: contracts whose part was missing last time (None
        # when not in an editor) and when the last reminder was shown.
        self._editor_missing: frozenset[str] | None = None
        self._editor_reminded = float("-inf")

    def tick(self) -> None:
        """Do one round of reading, deciding and acting.

        Raises:
            GameUnavailableError: When the game cannot be read; the caller
                should wait and retry.
        """
        now = self._clock()
        state = self._game.read_flight()
        if state is None:
            self._leave_flight()
            if now - self._last_idle_read < self._options.idle_refresh_seconds:
                return
            self._last_idle_read = now
            self._refresh_contracts(announce=False)
            self._log_idle_plan()
            editor = self._game.read_editor()
            if editor is None:
                self._editor_missing = None
            else:
                self._watch_editor(editor, now)
            return
        self._editor_missing = None

        self._refresh_contracts(announce=True)
        if self._flight is None or self._flight.vessel != state.vessel:
            self._start_flight(state)
        plan = plan_tests(list(self._contracts.values()), state, self._options.assisted_bodies)
        self._show_plan_if_changed(plan, state)

        for entry in plan:
            if entry.actionable:
                self._follow(entry, state, now)
        self._steer_throttle(plan, state, now)

    # ----------------------------------------------------------------- contracts

    def _refresh_contracts(self, announce: bool) -> None:
        """Read the contracts and report the ones that got completed.

        Args:
            announce: Show completions on screen (only in flight).
        """
        current = {c.requirement.contract_id: c for c in self._game.read_test_contracts()}
        for contract_id, contract in current.items():
            if contract.progress.done and contract_id not in self._finished:
                self._finished.add(contract_id)
                if announce:
                    self._announce_done(contract)
        for contract_id, previous in self._contracts.items():
            if contract_id in current or contract_id in self._finished:
                continue
            # The contract left the active list: completed (usually in the
            # same frame as its last objective), failed or cancelled.
            self._finished.add(contract_id)
            outcome = self._game.contract_state(contract_id)
            if outcome == "completed":
                if announce:
                    self._announce_done(previous)
            else:
                logger.info("Contract %r is no longer active (%s)",
                            previous.requirement.contract_title, outcome or "unknown")
        self._contracts = current

    def _announce_done(self, contract: TestContract) -> None:
        """Tell the player a test objective is complete.

        Args:
            contract: The contract.
        """
        requirement = contract.requirement
        verb = "Test" if requirement.kind is ObjectiveKind.TEST else "Haul"
        self._notify(Notice(f"{verb} complete: {requirement.part_title}",
                            NoticeLevel.SUCCESS, NoticePlacement.BANNER, 8.0))

    def _log_idle_plan(self) -> None:
        """Log the pending tests when they change, outside flight."""
        plan = plan_tests(list(self._contracts.values()), None, self._options.assisted_bodies)
        signature = tuple((e.requirement.contract_id, e.verdict) for e in plan)
        if signature == self._idle_signature:
            return
        self._idle_signature = signature
        pending = [e for e in plan if e.verdict is not Verdict.DONE]
        if not pending:
            logger.info("No pending part test contracts")
            return
        logger.info("Pending part tests:")
        for entry in pending:
            logger.info("  %s", _plan_line(entry))

    # ----------------------------------------------------------------- editor

    def _watch_editor(self, editor: EditorState, now: float) -> None:
        """Warn about the parts the pending tests need and the craft lacks.

        Warnings are shown when entering the editor, whenever the list
        changes and then every ``editor_reminder_seconds``. Adding a missing
        part is confirmed in green.

        Args:
            editor: The craft being designed.
            now: Current time.
        """
        if editor.parts is None:
            return
        missing = missing_parts(list(self._contracts.values()), editor.parts)
        ids = frozenset(c.requirement.contract_id for c in missing)
        previous = self._editor_missing
        if previous is not None:
            for contract in self._contracts.values():
                if contract.requirement.contract_id in previous - ids:
                    self._notify(Notice(f"Part on board: {contract.requirement.part_title}",
                                        NoticeLevel.SUCCESS, NoticePlacement.SIDE))
        reminder = self._options.editor_reminder_seconds
        due = reminder > 0 and now - self._editor_reminded >= reminder
        if missing and (ids != previous or due):
            self._editor_reminded = now
            for contract in missing:
                requirement = contract.requirement
                self._notify(Notice(
                    f"Missing part: {requirement.part_title} ({requirement.describe()})",
                    NoticeLevel.ERROR, NoticePlacement.SIDE, 10.0))
        self._editor_missing = ids

    # ----------------------------------------------------------------- flight

    def _start_flight(self, state: FlightState) -> None:
        """Reset the per-flight memory when a new vessel is flown.

        Args:
            state: The new vessel's state.
        """
        self._leave_flight()
        self._flight = _FlightMemory(vessel=state.vessel)
        logger.info("Flying %s", state.vessel)

    def _leave_flight(self) -> None:
        """Forget the flight (scene change or vessel switch)."""
        self._flight = None
        self._governor.reset()

    def _show_plan_if_changed(self, plan: list[PlanEntry], state: FlightState) -> None:
        """Show the plan at the start of a flight and whenever it changes.

        Args:
            plan: The current plan.
            state: The vessel's state.
        """
        flight = self._flight
        signature = tuple((e.requirement.contract_id, e.verdict, e.order) for e in plan)
        if flight is None or signature == flight.plan_signature:
            return
        flight.plan_signature = signature
        pending = [e for e in plan if e.verdict is not Verdict.DONE]
        if not pending:
            return
        logger.info("Part test plan for %s:", state.vessel)
        # Missing parts are only worth a warning on the launch pad, where
        # the player can still go back to the editor. Once flying, the
        # mission is what it is: contracts for parts this vessel does not
        # carry belong to other flights and stay out of the way.
        on_pad = state.situation is Situation.PRE_LAUNCH
        for entry in pending:
            logger.info("  %s", _plan_line(entry))
            if entry.verdict is Verdict.NOT_ASSISTED:
                continue  # left to the player: not even a message
            if entry.verdict is Verdict.MISSING_PART:
                if on_pad:
                    self._notify(Notice(f"Missing part: {entry.requirement.part_title}",
                                        NoticeLevel.ERROR, NoticePlacement.SIDE, 12.0),
                                 log=False)
            else:
                self._notify(Notice(_plan_line(entry), NoticeLevel.INFO,
                                    NoticePlacement.SIDE, 12.0), log=False)

    def _follow(self, entry: PlanEntry, state: FlightState, now: float) -> None:
        """Announce and act on one test of this flight.

        Args:
            entry: The test.
            state: The vessel's state.
            now: Current time.
        """
        flight = self._flight
        requirement = entry.requirement
        contract_id = requirement.contract_id
        if flight is None or contract_id in self._finished:
            return
        ours = check_conditions(requirement, state)

        # Announce the altitude window when entering it in the right place.
        right_place = ours.body and ours.situation
        in_altitude = right_place and requirement.altitude is not None and ours.altitude
        # Skipped when the game already says every condition holds: its
        # altitude can be a frame ahead of ours, and "stage now" then
        # supersedes "altitude reached".
        already_met = contract_id in flight.conditions_met or entry.contract.progress.conditions_met
        if in_altitude and contract_id not in flight.in_altitude and not already_met:
            self._notify(_altitude_notice(entry, state, ours.speed))
        if in_altitude:
            flight.in_altitude.add(contract_id)
        else:
            flight.in_altitude.discard(contract_id)

        # The game's verdict counts; ours is the fallback.
        met = entry.contract.progress.conditions_met
        met = ours.all if met is None else met
        was_met = contract_id in flight.conditions_met
        if met:
            flight.conditions_met.add(contract_id)
            self._act(entry, state, now)
        else:
            flight.conditions_met.discard(contract_id)
            if was_met and requirement.kind is ObjectiveKind.TEST:
                self._notify(Notice(f"Left the test window of {requirement.part_title}",
                                    NoticeLevel.WARNING, NoticePlacement.SIDE))

    def _act(self, entry: PlanEntry, state: FlightState, now: float) -> None:
        """Do what a test needs while all its conditions hold.

        Args:
            entry: The test.
            state: The vessel's state.
            now: Current time.
        """
        flight = self._flight
        requirement = entry.requirement
        contract_id = requirement.contract_id
        if flight is None or requirement.kind is ObjectiveKind.HAVE:
            return  # the game completes "have the part" objectives by itself
        part = next((p for p in state.parts or () if _same(p.title, requirement.part_title)), None)
        by_menu = requirement.activation is Activation.MENU or (
            requirement.activation is Activation.UNKNOWN and part is not None and part.can_run_test
        )
        if by_menu and self._options.run_tests:
            last = flight.last_run.get(contract_id)
            if last is None or now - last >= self._options.retry_seconds:
                flight.last_run[contract_id] = now
                if self._game.run_test(requirement.part_title):
                    self._notify(Notice(f"Running test of {requirement.part_title}",
                                        NoticeLevel.INFO, NoticePlacement.SIDE))
                else:
                    self._notify(Notice(f"Could not run the test of {requirement.part_title}: "
                                        "use Run Test in its menu", NoticeLevel.ACTION,
                                        NoticePlacement.BANNER, 4.0))
            return
        last = flight.last_banner.get(contract_id)
        if last is None or now - last >= self._options.repeat_seconds:
            flight.last_banner[contract_id] = now
            place = describe_place(requirement)
            if by_menu:
                text = f"Conditions met ({place}): use Run Test on {requirement.part_title}"
            else:
                stage = f" (stage {part.stage})" if part is not None and part.stage >= 0 else ""
                text = f"STAGE NOW: {requirement.part_title}{stage} - {place}"
            self._notify(Notice(text, NoticeLevel.ACTION, NoticePlacement.BANNER,
                                self._options.repeat_seconds))

    # ----------------------------------------------------------------- throttle

    def _steer_throttle(self, plan: list[PlanEntry], state: FlightState, now: float) -> None:
        """Hold the speed window of the current target with the throttle.

        Only while climbing in the atmosphere towards (or through) a test
        with a speed window: that is when the throttle decides the speed.
        Descending, the engines are usually off and the speed belongs to
        the parachutes and the drag.

        Args:
            plan: The flight plan.
            state: The vessel's state.
            now: Current time.
        """
        flight = self._flight
        if flight is None or not self._options.control_throttle:
            return
        target = select_target(plan, state)
        speed = target_speed_range(target, plan) if target is not None else None
        altitude = target.requirement.altitude if target is not None else None
        climbing = state.situation is Situation.FLYING and state.vertical_speed > 0
        below_top = altitude is None or state.altitude <= altitude.high
        contract_id = target.requirement.contract_id if target is not None else None
        wanted = (target is not None and speed is not None and climbing and below_top
                  and contract_id not in flight.released)

        if not wanted:
            if flight.governed is not None:
                self._notify(Notice("Throttle control released", NoticeLevel.INFO,
                                    NoticePlacement.SIDE))
            flight.governed = None
            flight.last_command = None
            self._governor.reset()
            return

        # Moving the throttle by hand means the player wants it back.
        if (flight.governed == contract_id and flight.last_command is not None
                and abs(state.throttle - flight.last_command) > 0.05):
            flight.released.add(contract_id)
            flight.governed = None
            flight.last_command = None
            self._governor.reset()
            self._notify(Notice("Throttle released: you moved it", NoticeLevel.INFO,
                                NoticePlacement.SIDE))
            return

        aim = speed.aim(self._options.speed_aim)
        if flight.governed != contract_id:
            flight.governed = contract_id
            self._governor.reset()
            self._notify(Notice(
                f"Throttle: holding {aim:.0f} m/s ({speed.low:.0f}-{speed.high:.0f}) "
                f"for {target.requirement.part_title}", NoticeLevel.INFO, NoticePlacement.SIDE))
        if (not flight.solid_warned and flight.last_command is not None
                and flight.last_command < 0.05 and state.thrust > 0.05 * max(state.available_thrust, 1.0)
                and state.surface_speed > speed.high):
            flight.solid_warned = True
            self._notify(Notice("Too fast and the thrust cannot be cut (solid boosters?)",
                                NoticeLevel.WARNING, NoticePlacement.SIDE))
        command = self._governor.command(aim, state, now)
        self._game.set_throttle(command)
        flight.last_command = command

    # ----------------------------------------------------------------- output

    def _notify(self, notice: Notice, log: bool = True) -> None:
        """Show a message in game and in the console.

        Args:
            notice: The message.
            log: Also write it to the log.
        """
        if log:
            logger.info("[%s] %s", notice.level.value, notice.text)
        self._game.show(notice)


def _same(first: str, second: str) -> bool:
    """Compare part titles loosely (case and spacing).

    Args:
        first: A title.
        second: Another title.

    Returns:
        True when they are the same title.
    """
    return " ".join(first.split()).casefold() == " ".join(second.split()).casefold()


def _altitude_notice(entry: PlanEntry, state: FlightState, speed_ok: bool) -> Notice:
    """Build the "test altitude reached" message.

    Args:
        entry: The test.
        state: The vessel's state.
        speed_ok: Whether the speed is inside its window.

    Returns:
        The message: yellow when the player will have to act.
    """
    requirement = entry.requirement
    speed = requirement.speed
    if speed is None or speed_ok:
        speed_text = "speed OK"
    elif state.surface_speed < speed.low:
        speed_text = f"too slow ({state.surface_speed:.0f} < {speed.low:.0f} m/s)"
    else:
        speed_text = f"too fast ({state.surface_speed:.0f} > {speed.high:.0f} m/s)"
    player_acts = requirement.kind is ObjectiveKind.TEST and requirement.activation is not Activation.MENU
    level = NoticeLevel.ACTION if player_acts else NoticeLevel.INFO
    return Notice(f"Test altitude reached for {requirement.part_title}: {speed_text}",
                  level, NoticePlacement.BANNER, 5.0)


def _plan_line(entry: PlanEntry) -> str:
    """Describe one plan entry in one line.

    Args:
        entry: The entry.

    Returns:
        Text such as ``"1. Mk16-XL (stage): flying at Kerbin, 2 km-8 km"``.
    """
    requirement = entry.requirement
    how = {
        ObjectiveKind.HAVE: "carry",
        ObjectiveKind.TEST: {"staging": "stage it", "menu": "Run Test"}.get(
            requirement.activation.value, "test"),
    }[requirement.kind]
    head = f"{entry.order}." if entry.order else {
        Verdict.ANOTHER_FLIGHT: "Another flight:",
        Verdict.MISSING_PART: "Missing part:",
        Verdict.NOT_ASSISTED: "Not assisted:",
        Verdict.DONE: "Done:",
    }.get(entry.verdict, "-")
    line = f"{head} {requirement.part_title} ({how}): {requirement.describe()}"
    return f"{line} - {entry.reason}" if entry.reason else line
