"""Tests for part test contracts: parsing, planning, throttle and assistant."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ksp_control.adapters.game.contract_text import parse_number, parse_requirement
from ksp_control.application.part_test_assistant import AssistantOptions, PartTestAssistant
from ksp_control.application.ports import Notice, NoticeLevel, NoticePlacement, PartTestGamePort
from ksp_control.domain.part_tests import (
    Activation,
    EditorState,
    FlightState,
    ObjectiveKind,
    PartTestRequirement,
    Range,
    RequirementProgress,
    Situation,
    SpeedGovernor,
    TestContract,
    TestPart,
    Verdict,
    check_conditions,
    plan_tests,
    select_target,
)

STAGING_NOTES = ("Para realizar la prueba, activa la pieza a través de la secuencia de etapas "
                 "una vez que se cumplan todas las condiciones de la prueba.")
MENU_NOTES = ("Para realizar la prueba, usa la opción [Ejecutar Prueba] en el menú contextual "
              "de la pieza una vez que se cumplan todas las condiciones para la prueba.")
CHUTE = "Paracaídas Mk16-XL"


def requirement(contract_id="c1", part=CHUTE, kind=ObjectiveKind.TEST,
                activation=Activation.STAGING, situation=Situation.FLYING,
                altitude=(2000, 8000), speed=(140, 270), body="Kerbin") -> PartTestRequirement:
    """Build a requirement with sensible defaults."""
    return PartTestRequirement(
        contract_id=contract_id, contract_title=f"Test {part}", part_title=part, kind=kind,
        activation=activation, body=body, situation=situation,
        altitude=Range(*altitude) if altitude else None,
        speed=Range(*speed) if speed else None,
    )


def contract(req: PartTestRequirement, done=False, met=None) -> TestContract:
    """Wrap a requirement with its progress."""
    return TestContract(req, RequirementProgress(done=done, conditions_met=met))


def flight(altitude=1000.0, speed=150.0, vertical=100.0, parts=(CHUTE,), **changes) -> FlightState:
    """Build a flight state of a rocket climbing over Kerbin."""
    state = FlightState(
        vessel="Probe", body="Kerbin", situation=Situation.FLYING, altitude=altitude,
        vertical_speed=vertical, surface_speed=speed, orbital_speed=speed + 170,
        throttle=1.0, thrust=200_000.0, available_thrust=200_000.0, mass=10_000.0,
        parts=tuple(TestPart(title, stage=2) for title in parts),
    )
    return replace(state, **changes)


# ------------------------------------------------------------------- parsing

def test_parses_the_real_parachute_contract():
    """Texts read from the game (Spanish, KSP 1.12.5, kRPC 0.8.0)."""
    req = parse_requirement(
        "7", "Comprueba Paracaídas Mk16-XL en vuelo sobre Kerbin.", "Probar Paracaídas Mk16-XL",
        STAGING_NOTES, ["Kerbin", "Volando", "Alt: 2,000m a 8,000m", "Vel: 140.0m/s a 270.0m/s"],
    )
    assert req == PartTestRequirement(
        contract_id="7", contract_title="Comprueba Paracaídas Mk16-XL en vuelo sobre Kerbin.",
        part_title=CHUTE, kind=ObjectiveKind.TEST, activation=Activation.STAGING,
        body="Kerbin", situation=Situation.FLYING,
        altitude=Range(2000, 8000), speed=Range(140, 270),
    )


def test_parses_haul_and_menu_objectives():
    """'Ten X' is a haul; menu notes mean Run Test; body markers are dropped."""
    haul = parse_requirement("1", "t", "Ten Batería Z-100", None, ["Muna^N", "Órbita"])
    assert (haul.kind, haul.part_title, haul.body, haul.situation, haul.altitude) == (
        ObjectiveKind.HAVE, "Batería Z-100", "Muna", Situation.ORBITING, None)
    test = parse_requirement("2", "t", "Test Z-100", MENU_NOTES, [])
    assert test.activation is Activation.MENU


def test_unknown_objective_is_not_a_part_test():
    """Other objective formats are ignored."""
    assert parse_requirement("1", "t", "Planta una bandera", None, []) is None


@pytest.mark.parametrize("text, value", [
    ("2,000", 2000), ("8.000", 8000), ("140.0", 140), ("1,234.5", 1234.5),
    ("1.234,5", 1234.5), ("12 500", 12500), ("70", 70), ("270,0", 270),
])
def test_parse_number_handles_every_culture(text, value):
    """Thousands separators have three digits after them; decimals do not."""
    assert parse_number(text) == value


# ------------------------------------------------------------------- domain

def test_conditions_use_surface_speed_while_flying():
    """Inside both windows means every condition holds."""
    req = requirement()
    assert check_conditions(req, flight(altitude=3000, speed=200)).all
    assert not check_conditions(req, flight(altitude=3000, speed=300)).speed
    assert not check_conditions(req, flight(altitude=9000, speed=200)).altitude


def test_plan_orders_by_altitude_and_merges_overlapping_windows():
    """Lower windows first; tests that can happen at once go together."""
    high = contract(requirement("high", part="A", altitude=(10_000, 20_000), speed=(300, 500)))
    low = contract(requirement("low", part="B", altitude=(2000, 8000)))
    overlap = contract(requirement("overlap", part="C", altitude=(5000, 9000), speed=(200, 400)))
    plan = plan_tests([high, low, overlap], flight(parts=("A", "B", "C")))
    assert [(e.requirement.contract_id, e.verdict, e.order) for e in plan] == [
        ("low", Verdict.THIS_FLIGHT, 1),
        ("overlap", Verdict.TOGETHER, 1),
        ("high", Verdict.THIS_FLIGHT, 2),
    ]


def test_plan_flags_missing_parts_and_used_up_parts():
    """A staged part fires once: a second disjoint test needs another flight."""
    first = contract(requirement("first", altitude=(2000, 4000)))
    second = contract(requirement("second", altitude=(10_000, 12_000), speed=(400, 500)))
    missing = contract(requirement("missing", part="Z"))
    plan = {e.requirement.contract_id: e for e in plan_tests([first, second, missing], flight())}
    assert plan["first"].verdict is Verdict.THIS_FLIGHT
    assert plan["second"].verdict is Verdict.ANOTHER_FLIGHT
    assert "staging uses it up" in plan["second"].reason
    assert plan["missing"].verdict is Verdict.MISSING_PART


def test_plan_menu_tests_reuse_the_part_and_other_bodies_wait():
    """Run Test does not consume the part; tests at other bodies are not assisted."""
    a = contract(requirement("a", activation=Activation.MENU, altitude=(2000, 4000)))
    b = contract(requirement("b", activation=Activation.MENU, altitude=(10_000, 12_000), speed=None))
    mun = contract(requirement("mun", body="Muna", situation=Situation.LANDED, altitude=None, speed=None))
    plan = {e.requirement.contract_id: e.verdict for e in plan_tests([a, b, mun], flight())}
    assert plan == {"a": Verdict.THIS_FLIGHT, "b": Verdict.THIS_FLIGHT, "mun": Verdict.NOT_ASSISTED}


def test_target_is_the_next_window_in_the_direction_of_travel():
    """Climbing heads up, falling heads down."""
    low = contract(requirement("low", part="B", altitude=(2000, 4000)))
    high = contract(requirement("high", part="A", altitude=(10_000, 12_000), speed=(400, 500)))
    state = flight(altitude=6000, parts=("A", "B"))
    plan = plan_tests([low, high], state)
    assert select_target(plan, state).requirement.contract_id == "high"
    falling = replace(state, vertical_speed=-50)
    assert select_target(plan, falling).requirement.contract_id == "low"


def test_governor_holds_the_speed_inside_the_window():
    """Simulated climb with gravity and drag: the speed settles in range."""
    governor = SpeedGovernor()
    state = flight(altitude=0, speed=0, throttle=1.0)
    dt, mass, max_thrust = 0.25, 10_000.0, 250_000.0
    for step in range(400):
        throttle = governor.command(190.0, state, step * dt)
        thrust = throttle * max_thrust
        drag = 0.4 * state.surface_speed ** 2
        accel = (thrust - drag) / mass - 9.81 * 0.8
        speed = max(0.0, state.surface_speed + accel * dt)
        state = replace(state, surface_speed=speed, throttle=throttle, thrust=thrust,
                        available_thrust=max_thrust, mass=mass)
    assert 170 <= state.surface_speed <= 210


# ------------------------------------------------------------------- assistant

class FakeGame(PartTestGamePort):
    """In-memory game for the assistant tests."""

    def __init__(self, contracts: list[TestContract], state: FlightState | None) -> None:
        """Start with some contracts and a flight state."""
        self.contracts = contracts
        self.state = state
        self.notices: list[Notice] = []
        self.throttle: list[float] = []
        self.tests_run: list[str] = []
        self.states: dict[str, str] = {}
        self.editor: EditorState | None = None

    def read_test_contracts(self) -> list[TestContract]:
        """Return the current contracts."""
        return list(self.contracts)

    def contract_state(self, contract_id: str) -> str | None:
        """Return the configured final state."""
        return self.states.get(contract_id)

    def read_flight(self) -> FlightState | None:
        """Return the current flight state."""
        return self.state

    def read_editor(self) -> EditorState | None:
        """Return the craft being designed."""
        return self.editor

    def set_throttle(self, value: float) -> None:
        """Record the throttle and apply it to the state."""
        self.throttle.append(value)
        self.state = replace(self.state, throttle=value)

    def run_test(self, part_title: str) -> bool:
        """Record the test."""
        self.tests_run.append(part_title)
        return True

    def show(self, notice: Notice) -> None:
        """Record the message."""
        self.notices.append(notice)

    def close(self) -> None:
        """Nothing to close."""

    def texts(self, level: NoticeLevel | None = None) -> list[str]:
        """Messages shown so far, optionally of one level."""
        return [n.text for n in self.notices if level is None or n.level is level]


class Clock:
    """Manual clock."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the time."""
        return self.now


def test_staging_test_warns_in_yellow_and_completion_in_green():
    """The player stages; the assistant only says when."""
    req = requirement()
    game = FakeGame([contract(req, met=False)], flight(altitude=1000, speed=200))
    clock = Clock()
    assistant = PartTestAssistant(game, AssistantOptions(control_throttle=False), clock)
    assistant.tick()
    assert any("Paracaídas Mk16-XL" in t for t in game.texts(NoticeLevel.INFO))  # the plan

    game.state = flight(altitude=2500, speed=200)
    clock.now = 1
    assistant.tick()  # in the altitude window, the game not yet convinced
    game.contracts = [contract(req, met=True)]
    clock.now = 1.25
    assistant.tick()
    actions = [n for n in game.notices if n.level is NoticeLevel.ACTION]
    assert any(t.startswith("Test altitude reached") for t in (n.text for n in actions))
    assert any(n.text.startswith("STAGE NOW") and n.placement is NoticePlacement.BANNER
               for n in actions)
    assert game.tests_run == []

    game.contracts = []
    game.states["c1"] = "completed"
    clock.now = 2
    assistant.tick()
    assert game.texts(NoticeLevel.SUCCESS) == [f"Test complete: {CHUTE}"]


def test_menu_test_is_run_by_the_assistant_once_conditions_hold():
    """Run Test is pressed when the game says the conditions hold."""
    req = requirement(activation=Activation.MENU)
    game = FakeGame([contract(req, met=True)], flight(altitude=3000, speed=200))
    PartTestAssistant(game, AssistantOptions(control_throttle=False), Clock()).tick()
    assert game.tests_run == [CHUTE]


def test_missing_part_is_a_side_warning_on_the_launch_pad():
    """No part on board: small warning, never a banner."""
    game = FakeGame([contract(requirement())],
                    flight(parts=(), situation=Situation.PRE_LAUNCH, vertical=0, speed=0))
    PartTestAssistant(game, AssistantOptions(control_throttle=False), Clock()).tick()
    (warning,) = [n for n in game.notices if n.level is NoticeLevel.ERROR]
    assert warning.placement is NoticePlacement.SIDE
    assert warning.text == f"Missing part: {CHUTE}"


def test_missing_parts_are_not_reported_once_flying():
    """After launch, contracts for parts not on board stay silent."""
    game = FakeGame([contract(requirement())], flight(parts=()))
    PartTestAssistant(game, AssistantOptions(control_throttle=False), Clock()).tick()
    assert game.texts(NoticeLevel.ERROR) == []


def test_throttle_is_governed_while_climbing_and_released_by_hand():
    """Too fast below the window: throttle down; a manual change wins."""
    game = FakeGame([contract(requirement())], flight(altitude=1000, speed=260))
    clock = Clock()
    assistant = PartTestAssistant(game, AssistantOptions(), clock)
    assistant.tick()
    clock.now = 0.25
    game.state = replace(game.state, surface_speed=262)
    assistant.tick()
    assert game.throttle and game.throttle[-1] < 1.0

    game.state = replace(game.state, throttle=1.0)  # the player pushes it up
    count = len(game.throttle)
    clock.now = 0.5
    assistant.tick()
    clock.now = 0.75
    assistant.tick()
    assert len(game.throttle) == count
    assert "Throttle released: you moved it" in game.texts()


def test_throttle_is_left_alone_when_descending():
    """Falling through the window: no throttle commands."""
    game = FakeGame([contract(requirement())], flight(altitude=9000, speed=200, vertical=-80))
    PartTestAssistant(game, AssistantOptions(), Clock()).tick()
    assert game.throttle == []


def test_launch_pad_staging_test_says_where():
    """Situation-only tests: the banner names the place, no altitude talk."""
    jet = 'Motor Turbofan J-33 "Wheesley"'
    req = requirement(part=jet, situation=Situation.PRE_LAUNCH, altitude=None, speed=None)
    state = flight(altitude=70, speed=0, vertical=0, parts=(jet,), situation=Situation.PRE_LAUNCH)
    game = FakeGame([contract(req, met=True)], state)
    PartTestAssistant(game, AssistantOptions(), Clock()).tick()
    assert f"STAGE NOW: {jet} (stage 2) - on the launch pad" in game.texts(NoticeLevel.ACTION)
    assert game.throttle == []


def test_menu_situation_test_runs_when_landed():
    """A Run Test part is tested as soon as the game says it is landed."""
    req = requirement(activation=Activation.MENU, situation=Situation.LANDED,
                      altitude=None, speed=None)
    state = flight(vertical=0, speed=0, situation=Situation.LANDED)
    game = FakeGame([contract(req, met=False)], state)
    clock = Clock()
    assistant = PartTestAssistant(game, AssistantOptions(), clock)
    assistant.tick()
    assert game.tests_run == []
    game.contracts = [contract(req, met=True)]
    clock.now = 1
    assistant.tick()
    assert game.tests_run == [CHUTE]


def test_editor_warns_in_red_about_missing_parts_and_confirms_additions():
    """Small red warnings while designing; green when the part is added."""
    jet = 'Motor Turbofan J-33 "Wheesley"'
    contracts = [contract(requirement()), contract(requirement("c2", part=jet, altitude=None,
                                                               speed=None))]
    game = FakeGame(contracts, None)
    game.editor = EditorState("Rocket", (TestPart(jet),))
    clock = Clock()
    assistant = PartTestAssistant(game, AssistantOptions(editor_reminder_seconds=30), clock)
    assistant.tick()
    (warning,) = [n for n in game.notices if n.level is NoticeLevel.ERROR]
    assert warning.placement is NoticePlacement.SIDE
    assert warning.text.startswith(f"Missing part: {CHUTE}")

    clock.now = 5  # nothing changed, no reminder due yet
    assistant.tick()
    assert len(game.texts(NoticeLevel.ERROR)) == 1
    clock.now = 31  # reminder
    assistant.tick()
    assert len(game.texts(NoticeLevel.ERROR)) == 2

    game.editor = EditorState("Rocket", (TestPart(jet), TestPart(CHUTE)))
    clock.now = 40
    assistant.tick()
    assert game.texts(NoticeLevel.SUCCESS) == [f"Part on board: {CHUTE}"]
    assert len(game.texts(NoticeLevel.ERROR)) == 2


def test_tests_at_other_bodies_are_left_alone_even_when_there():
    """Flying in Duna's atmosphere for a Duna test: no throttle, no messages."""
    req = requirement(body="Duna", activation=Activation.MENU)
    state = flight(altitude=3000, speed=260, parts=(CHUTE,), body="Duna")
    game = FakeGame([contract(req, met=True)], state)
    PartTestAssistant(game, AssistantOptions(), Clock()).tick()
    assert (game.throttle, game.tests_run, game.notices) == ([], [], [])
    (entry,) = plan_tests([contract(req)], state)
    assert entry.verdict is Verdict.NOT_ASSISTED


def test_tests_at_other_bodies_do_not_warn_about_parts_on_the_pad():
    """A missing part for a Mun test is not this mission's business."""
    req = requirement(body="Muna", situation=Situation.LANDED, altitude=None, speed=None)
    pad = flight(parts=(), situation=Situation.PRE_LAUNCH, vertical=0, speed=0)
    game = FakeGame([contract(req)], pad)
    PartTestAssistant(game, AssistantOptions(), Clock()).tick()
    assert game.notices == []


def test_no_altitude_notice_when_the_game_is_already_satisfied():
    """"Stage now" supersedes "altitude reached" when both arrive together."""
    game = FakeGame([contract(requirement(), met=True)], flight(altitude=2500, speed=200))
    PartTestAssistant(game, AssistantOptions(control_throttle=False), Clock()).tick()
    texts = game.texts(NoticeLevel.ACTION)
    assert texts and all(t.startswith("STAGE NOW") for t in texts)


# ------------------------------------------------------------------- kRPC adapter structure

class _Remote:
    """Minimal stand-in for kRPC remote objects (hashable, attribute bag)."""

    def __init__(self, **attributes) -> None:
        """Store the attributes."""
        self.__dict__.update(attributes)


def _param(title, completed=False, children=()):
    """A fake contract parameter."""
    return _Remote(title=title, completed=completed, children=list(children), notes=None)


def _fake_game(contracts):
    """A KrpcPartTestGame whose connection returns the given contracts."""
    from ksp_control.adapters.game.krpc_part_tests import KrpcPartTestGame
    from ksp_control.adapters.game.krpc_connection import KrpcEndpoint

    game = KrpcPartTestGame(KrpcEndpoint())
    manager = _Remote(active_contracts=contracts)
    game._conn = _Remote(space_center=_Remote(contract_manager=manager))
    return game


def test_haul_conditions_are_read_next_to_the_objective():
    """Real haul layout: conditions are siblings; 'Have X' completes early."""
    haul = _Remote(type="Contracts.Templates.PartTest", title="Transporta Flea", _object_id=5,
                   parameters=[
                       _param('Ten Cohete RT-5 "Flea"', completed=True),
                       _param("Kerbin", True), _param("Volando"),
                       _param("Alt: 31,000m a 39,000m"), _param("Vel: 350.0m/s a 510.0m/s"),
                   ])
    (read,) = _fake_game([haul]).read_test_contracts()
    assert read.requirement.kind is ObjectiveKind.HAVE
    assert (read.requirement.altitude, read.requirement.speed) == (Range(31000, 39000),
                                                                   Range(350, 510))
    assert read.progress == RequirementProgress(done=False, conditions_met=False)


def test_test_conditions_are_read_under_the_objective():
    """Real test layout: conditions are children of 'Test X'."""
    test = _Remote(type="Contracts.Templates.PartTest", title="Comprueba", _object_id=6,
                   parameters=[_param(f"Probar {CHUTE}", children=[
                       _param("Kerbin", True), _param("Volando", True),
                       _param("Alt: 2,000m a 8,000m", True), _param("Vel: 140.0m/s a 270.0m/s", True),
                   ])])
    (read,) = _fake_game([test]).read_test_contracts()
    assert read.requirement.altitude == Range(2000, 8000)
    assert read.progress == RequirementProgress(done=False, conditions_met=True)
