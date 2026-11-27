"""Invented people's names, to name vessels ("Cruise John Doe", relay probes).

Faker gives a large variety of realistic names; a short built-in list
stands in when it is not installed, so naming never stops a script.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable

logger = logging.getLogger(__name__)

_FIRST = ["John", "Jane", "Alex", "Maria", "Sam", "Laura", "Peter", "Anna", "Mark", "Eva"]
_LAST = ["Doe", "Smith", "Garcia", "Brown", "Miller", "Lopez", "Clark", "Young"]


def person_names(locale: str | None = None) -> Callable[[], str]:
    """Build a source of invented "First Last" names.

    Only the first and last name are used: Faker's full names may carry
    titles ("Dr.") or suffixes ("MD").

    Args:
        locale: Faker locale such as ``en_US`` or ``es_ES``; ``None`` uses
            Faker's default (English names).

    Returns:
        A function giving a new name on each call.
    """
    try:
        from faker import Faker
    except ImportError:  # pragma: no cover - environment issue
        logger.warning("Faker is not installed: using a short built-in list of names")
        return lambda: f"{random.choice(_FIRST)} {random.choice(_LAST)}"
    faker = Faker(locale) if locale else Faker()
    return lambda: f"{faker.first_name()} {faker.last_name()}"
