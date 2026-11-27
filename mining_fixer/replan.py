"""Scope-change re-plan bookkeeping: coalesce requests, cache results.

Pure (QUALITY Q-S1): no ``anki``/``aqt``/PyQt imports. Both dialogs run the
actual plan on Anki's background task manager and feed each completion back
here; this module owns only the state that must behave identically in both.

The user-visible contract:

- the checkbox is instant — requesting a scope never plans on the UI thread;
- a burst of toggles costs at most one running plan plus one queued state
  (the newest wins), not one plan per click;
- a state already planned in this dialog is served from cache, so toggling
  back is immediate;
- a result that no longer matches the newest request is still cached (it is a
  valid plan for its own state) but never shown.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplanRequest[T]:
    """One scope state to plan.

    ``cached`` is set when the state was already planned and no worker is
    needed. Otherwise ``start`` says whether the caller must start a worker,
    and ``token`` identifies that run; a request made while a worker runs has
    ``start=False`` and is remembered by the queue as the newest state.
    """

    state: bool
    token: int = 0
    cached: T | None = None
    start: bool = False


class ReplanQueue[T]:
    """Serialise re-plans for one dialog; the newest request wins."""

    def __init__(self) -> None:
        self._cache: dict[bool, T] = {}
        self._running = False
        self._generation = 0
        self._latest: bool | None = None

    def request(self, state: bool) -> ReplanRequest[T]:
        """Ask for the plan of *state* (``fix_neighbours`` on/off)."""
        self._latest = state
        cached = self._cache.get(state)
        if cached is not None:
            return ReplanRequest(state=state, cached=cached)
        if self._running:
            return ReplanRequest(state=state)
        self._running = True
        self._generation += 1
        return ReplanRequest(state=state, token=self._generation, start=True)

    def finish(
        self, token: int, state: bool, result: T
    ) -> tuple[T | None, ReplanRequest[T] | None]:
        """Record a worker result.

        Returns ``(result_to_show, next_request)``. ``result_to_show`` is None
        when the result is stale (a newer scope was requested meanwhile).
        ``next_request`` starts the newest still-unplanned state, if any.
        """
        self._running = False
        self._cache[state] = result
        shown = result if token == self._generation and state == self._latest else None
        return shown, self._next()

    def fail(self, token: int, state: bool) -> ReplanRequest[T] | None:
        """Record a failed worker run; never auto-retry the same state.

        A different state requested while the failed worker ran still starts;
        retrying the failed state needs a new ``request`` (a user toggle), so
        a persistent failure cannot spin.
        """
        self._running = False
        if self._latest is not None and self._latest != state:
            return self._next()
        return None

    def _next(self) -> ReplanRequest[T] | None:
        if self._latest is None or self._latest in self._cache:
            return None
        self._running = True
        self._generation += 1
        return ReplanRequest(state=self._latest, token=self._generation, start=True)
