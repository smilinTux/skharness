"""Derived lineage view over immutable experiment records."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

from .models import Experiment


class LineageGraph:
    def __init__(self, experiments: Iterable[Experiment] = ()):
        self._experiments: dict[str, Experiment] = {}
        self._children: dict[str, set[str]] = defaultdict(set)
        for experiment in experiments:
            self.add(experiment)

    def add(self, experiment: Experiment) -> None:
        previous = self._experiments.get(experiment.id)
        if previous is not None and previous.content_hash != experiment.content_hash:
            raise ValueError(f"experiment id {experiment.id!r} has conflicting content")
        if experiment.parent_id == experiment.id:
            raise ValueError("self-parent lineage is forbidden")
        if experiment.parent_id and self._would_cycle(experiment.id, experiment.parent_id):
            raise ValueError("experiment lineage would contain a cycle")
        self._experiments[experiment.id] = experiment
        if experiment.parent_id:
            self._children[experiment.parent_id].add(experiment.id)

    def _would_cycle(self, child_id: str, parent_id: str) -> bool:
        cursor: str | None = parent_id
        visited: set[str] = set()
        while cursor is not None:
            if cursor == child_id:
                return True
            if cursor in visited:
                return True
            visited.add(cursor)
            parent = self._experiments.get(cursor)
            cursor = parent.parent_id if parent else None
        return False

    def ancestors(self, experiment_id: str) -> tuple[Experiment, ...]:
        ancestors: list[Experiment] = []
        cursor = self._experiments[experiment_id].parent_id
        seen: set[str] = set()
        while cursor is not None:
            if cursor in seen:
                raise ValueError("experiment lineage contains a cycle")
            seen.add(cursor)
            parent = self._experiments.get(cursor)
            if parent is None:
                break
            ancestors.append(parent)
            cursor = parent.parent_id
        return tuple(ancestors)

    def descendants(self, experiment_id: str) -> tuple[Experiment, ...]:
        found: list[Experiment] = []
        queue = deque(sorted(self._children.get(experiment_id, ())))
        while queue:
            child_id = queue.popleft()
            found.append(self._experiments[child_id])
            queue.extend(sorted(self._children.get(child_id, ())))
        return tuple(found)

    def roots(self) -> tuple[Experiment, ...]:
        return tuple(
            sorted(
                (item for item in self._experiments.values() if item.parent_id is None),
                key=lambda item: item.id,
            )
        )
