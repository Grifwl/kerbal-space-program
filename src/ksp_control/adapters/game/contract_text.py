"""Understand the text of KSP part test contracts.

kRPC only exposes contract objectives as display text, so the conditions
have to be read back from it. The formats come from KSP 1.12's own code
and localization files (verified on a Spanish game):

* objective: ``"Probar <part>"`` / ``"Test <part>"`` (activate it) or
  ``"Ten <part>"`` / ``"Have <part>"`` (carry it);
* objective notes: how to trigger the test, through the staging sequence
  or the "Run Test" entry of the part's menu;
* sub-objectives, in this order: the body (``"Kerbin"``), the situation
  (``"Volando"``), ``"Alt: 2,000m a 8,000m"`` and
  ``"Vel: 140.0m/s a 270.0m/s"``. Ranges are formatted with the invariant
  culture, but other separators are accepted too.

Anything not understood is left as ``None``; the game's own completion
flags still drive the assistant in that case.
"""

from __future__ import annotations

import re

from ksp_control.domain.part_tests import (
    Activation,
    ObjectiveKind,
    PartTestRequirement,
    Range,
    Situation,
)

PART_TEST_CONTRACT_TYPE = "Contracts.Templates.PartTest"

_TEST_PREFIXES = ("probar ", "test ")
_HAVE_PREFIXES = ("ten ", "have ")

# Situation names (ReachSituation titles) in the supported languages.
_SITUATIONS = {
    "volando": Situation.FLYING, "flying": Situation.FLYING,
    "aterrizado": Situation.LANDED, "landed": Situation.LANDED,
    "amerizado": Situation.SPLASHED, "splashed": Situation.SPLASHED,
    "splashed down": Situation.SPLASHED,
    "orbita": Situation.ORBITING, "órbita": Situation.ORBITING,
    "orbiting": Situation.ORBITING, "orbit": Situation.ORBITING,
    "trayectoria suborbital": Situation.SUB_ORBITAL,
    "sub-orbital trajectory": Situation.SUB_ORBITAL,
    "suborbital trajectory": Situation.SUB_ORBITAL, "sub-orbital": Situation.SUB_ORBITAL,
    "trayectoria de escape": Situation.ESCAPING, "escaping": Situation.ESCAPING,
    "escape trajectory": Situation.ESCAPING,
    "lugar de lanzamiento": Situation.PRE_LAUNCH, "launch site": Situation.PRE_LAUNCH,
    "prelaunch": Situation.PRE_LAUNCH,
}

_NUMBER = r"-?\d[\d.,\s  ]*"
# "<label>: <n>m <word> <n>m" -- the label and the joining word are
# localized, the unit is not.
_ALTITUDE = re.compile(rf"^[^\d-]*?(?P<low>{_NUMBER})\s*m\s+\S+\s+(?P<high>{_NUMBER})\s*m\s*$")
_SPEED = re.compile(rf"^[^\d-]*?(?P<low>{_NUMBER})\s*m/s\s+\S+\s+(?P<high>{_NUMBER})\s*m/s\s*$")

# Words that identify each way of triggering a test in the objective notes.
_STAGING_WORDS = ("etapas", "staging")
_MENU_WORDS = ("menú contextual", "ejecutar prueba", "run test", "context menu", "right-click")


def parse_number(text: str) -> float:
    """Parse a number formatted with any thousands/decimal separators.

    A separator followed by exactly three digits is a thousands
    separator; any other is the decimal point. That holds for KSP's
    ``N0`` (no decimals) and ``N1`` (one decimal) formats in every culture.

    Args:
        text: For example ``"2,000"``, ``"8.000"``, ``"140.0"`` or ``"1.234,5"``.

    Returns:
        The value.

    Raises:
        ValueError: When the text is not a number.
    """
    compact = re.sub(r"[\s  ]", "", text)
    sign = -1.0 if compact.startswith("-") else 1.0
    compact = compact.lstrip("-")
    pieces = re.split(r"[.,]", compact)
    if not pieces or not all(p.isdigit() for p in pieces):
        raise ValueError(f"Not a number: {text!r}")
    integer, decimals = pieces[0], ""
    for piece in pieces[1:]:
        if len(piece) == 3 and not decimals:
            integer += piece
        else:
            decimals = piece
    return sign * float(f"{integer}.{decimals or '0'}")


def parse_range(title: str, unit_pattern: re.Pattern[str]) -> Range | None:
    """Read a ``"<label>: <low><unit> <word> <high><unit>"`` title.

    Args:
        title: Sub-objective title.
        unit_pattern: ``_ALTITUDE`` or ``_SPEED``.

    Returns:
        The range, or ``None`` when the title has another format.
    """
    match = unit_pattern.match(title.strip())
    if match is None:
        return None
    try:
        return Range(parse_number(match["low"]), parse_number(match["high"]))
    except ValueError:
        return None


def parse_activation(notes: str | None) -> Activation:
    """Tell from the objective notes how the test is triggered.

    Args:
        notes: Notes of the "Test <part>" objective.

    Returns:
        Staging, menu, or unknown.
    """
    text = (notes or "").casefold()
    if any(word in text for word in _STAGING_WORDS):
        return Activation.STAGING
    if any(word in text for word in _MENU_WORDS):
        return Activation.MENU
    return Activation.UNKNOWN


def parse_objective(title: str) -> tuple[ObjectiveKind, str] | None:
    """Split a "Test <part>" / "Have <part>" objective title.

    Args:
        title: Objective title.

    Returns:
        ``(kind, part title)``, or ``None`` when not recognised.
    """
    lowered = title.casefold()
    for prefixes, kind in ((_TEST_PREFIXES, ObjectiveKind.TEST), (_HAVE_PREFIXES, ObjectiveKind.HAVE)):
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return kind, title[len(prefix):].strip()
    return None


def parse_requirement(
    contract_id: str,
    contract_title: str,
    objective_title: str,
    objective_notes: str | None,
    condition_titles: list[str],
) -> PartTestRequirement | None:
    """Build a requirement from the texts of a part test contract.

    Args:
        contract_id: Identifier of the contract.
        contract_title: Contract title.
        objective_title: Title of its main objective.
        objective_notes: Notes of the main objective.
        condition_titles: Titles of the sub-objectives (the conditions).

    Returns:
        The requirement, or ``None`` when the objective is not a part test.
    """
    objective = parse_objective(objective_title)
    if objective is None:
        return None
    kind, part_title = objective
    body = situation = altitude = speed = None
    for raw in condition_titles:
        title = raw.strip()
        if speed is None and (parsed := parse_range(title, _SPEED)) is not None:
            speed = parsed
        elif altitude is None and (parsed := parse_range(title, _ALTITUDE)) is not None:
            altitude = parsed
        elif situation is None and title.casefold() in _SITUATIONS:
            situation = _SITUATIONS[title.casefold()]
        elif body is None and title and not any(ch.isdigit() for ch in title):
            # The body sub-objective is just the body's display name; KSP
            # may append grammatical markers like "^N".
            body = title.split("^", 1)[0].strip()
    return PartTestRequirement(
        contract_id=contract_id,
        contract_title=contract_title,
        part_title=part_title,
        kind=kind,
        activation=parse_activation(objective_notes) if kind is ObjectiveKind.TEST else Activation.UNKNOWN,
        body=body,
        situation=situation,
        altitude=altitude,
        speed=speed,
    )
