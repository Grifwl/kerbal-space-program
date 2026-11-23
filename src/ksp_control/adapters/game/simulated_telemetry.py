"""Simulated game, used for demos and for trying the dashboard without KSP.

The simulator plays a tiny career in a loop: accept contracts, build and
launch rockets, complete or fail contracts, recover or lose vessels, spend
science in R&D... It exercises every branch of the ledger rules, so it is
also a convenient end-to-end check of the whole application.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

from ksp_control.application.ports import GameTelemetryPort
from ksp_control.domain.model import (
    ContractBook,
    ContractTerms,
    GameScene,
    GameSnapshot,
    Resources,
)

_CONTRACT_TEMPLATES = [
    ("PartTest", "Test RT-10 \"Hammer\" at the launch pad", 3_000, 9_000, 4_000, 2, 6),
    ("ExplorationContract", "Orbit Kerbin", 8_000, 32_000, 12_000, 6, 15),
    ("SurveyContract", "Gather scientific data near the Mun", 12_000, 41_000, 15_000, 18, 20),
    ("RecoverAsset", "Rescue Bill from orbit of Kerbin", 6_000, 27_000, 10_000, 0, 12),
    ("SatelliteContract", "Put a satellite in a polar orbit", 15_000, 58_000, 22_000, 10, 25),
]
_VESSELS = [("Kerbal X", 34_000), ("Mun Lander I", 61_000), ("Comsat Mk1", 28_000)]


class SimulatedTelemetry(GameTelemetryPort):
    """A fake KSP career that evolves a little on every poll.

    Responsibilities: produce realistic ``GameSnapshot`` sequences, including
    scene transitions and contract state changes, without any game.

    Usage notes: deterministic for a given seed. ``steps_per_action`` slows
    the story down so a human can follow it in the dashboard.
    """

    def __init__(self, seed: int = 42, steps_per_action: int = 3) -> None:
        """Create a new simulated career.

        Args:
            seed: Random seed, for reproducible runs.
            steps_per_action: Number of polls between two game actions.
        """
        self._random = random.Random(seed)
        self._steps_per_action = max(1, steps_per_action)
        self._step = 0
        self._ut = 0.0
        self._scene = GameScene.SPACE_CENTER
        self._vessel: str | None = None
        self._resources = Resources(funds=150_000, science=12, reputation=20)
        self._active: list[ContractTerms] = []
        self._completed: list[ContractTerms] = []
        self._failed: list[ContractTerms] = []
        self._story: Iterator[Callable[[], None]] = self._career()

    def read_snapshot(self) -> GameSnapshot:
        """Advance the simulation and return the new state.

        Returns:
            The simulated snapshot.
        """
        self._step += 1
        self._ut += 60 * 15  # fifteen game minutes per poll
        if self._step % self._steps_per_action == 0:
            next(self._story)()
        return GameSnapshot(
            taken_at=datetime.now(timezone.utc),
            universal_time=self._ut,
            scene=self._scene,
            resources=self._resources,
            active_vessel=self._vessel,
            contracts=ContractBook(
                tuple(self._active), tuple(self._completed), tuple(self._failed)
            ),
        )

    def close(self) -> None:
        """Nothing to release."""

    def _career(self) -> Iterator[Callable[[], None]]:
        """Generate an endless sequence of game actions.

        Yields:
            Callables that mutate the simulated state.
        """
        while True:
            yield self._accept_contract
            yield self._accept_contract
            yield lambda: self._go_to(GameScene.EDITOR_VAB)
            name, cost = self._random.choice(_VESSELS)
            yield lambda: self._launch(name, cost)
            yield self._finish_contract
            if self._random.random() < 0.2:
                # Sometimes the rocket explodes on the pad: revert to the VAB.
                yield lambda: self._revert(cost)
                yield lambda: self._go_to(GameScene.SPACE_CENTER)
                continue
            yield lambda: self._gain(Resources(science=self._random.uniform(5, 30)))
            yield lambda: self._recover(cost * self._random.uniform(0.2, 0.8))
            yield lambda: self._gain(Resources(science=-self._random.choice([0, 0, 15, 45])))
            if self._random.random() < 0.05:
                yield lambda: self._gain(Resources(funds=-75_000))  # facility upgrade

    def _go_to(self, scene: GameScene) -> None:
        """Switch scene.

        Args:
            scene: Destination scene.
        """
        self._scene = scene
        if scene is not GameScene.FLIGHT:
            self._vessel = None

    def _gain(self, delta: Resources) -> None:
        """Change the balances, clamping science at zero.

        Args:
            delta: Change to apply.
        """
        new = self._resources + delta
        self._resources = Resources(new.funds, max(new.science, 0.0), new.reputation)

    def _accept_contract(self) -> None:
        """Accept a random contract and receive its advance."""
        kind, title, advance, reward, penalty, science, reputation = self._random.choice(
            _CONTRACT_TEMPLATES
        )
        # Small random variation, like KSP's contract generator.
        factor = self._random.uniform(0.8, 1.3)
        terms = ContractTerms(
            kind, title, round(advance * factor), round(reward * factor),
            round(penalty * factor), science, reputation, reputation,
        )
        self._active.append(terms)
        self._gain(terms.advance)

    def _launch(self, vessel: str, cost: float) -> None:
        """Roll a vessel out to the pad, paying for it.

        Args:
            vessel: Vessel name.
            cost: Vessel cost.
        """
        self._go_to(GameScene.FLIGHT)
        self._vessel = vessel
        self._gain(Resources(funds=-cost))

    def _revert(self, cost: float) -> None:
        """Revert the flight to the VAB, getting the vessel cost back.

        Args:
            cost: Vessel cost to refund.
        """
        self._go_to(GameScene.EDITOR_VAB)
        self._gain(Resources(funds=cost))

    def _recover(self, value: float) -> None:
        """Recover the active vessel and return to the space center.

        Args:
            value: Recovery value paid by KSP.
        """
        self._go_to(GameScene.SPACE_CENTER)
        self._gain(Resources(funds=value))

    def _finish_contract(self) -> None:
        """Complete (usually) or fail the oldest active contract."""
        if not self._active:
            return
        terms = self._active.pop(0)
        if self._random.random() < 0.8:
            self._completed.append(terms)
            # KSP scales reputation non-linearly; simulate a smaller gain.
            reward = terms.reward
            self._gain(Resources(reward.funds, reward.science, reward.reputation * 0.8))
        else:
            self._failed.append(terms)
            self._gain(terms.penalty)
