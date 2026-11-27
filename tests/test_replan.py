"""ReplanQueue: coalescing, caching and stale-result rules (pure, Q-S1)."""

from __future__ import annotations

from mining_fixer.replan import ReplanQueue


def test_first_request_starts_a_worker():
    queue: ReplanQueue[str] = ReplanQueue()
    request = queue.request(True)
    assert request.start is True
    assert request.token > 0
    assert request.cached is None


def test_a_finished_state_is_served_from_cache_without_a_worker():
    queue: ReplanQueue[str] = ReplanQueue()
    first = queue.request(True)
    shown, following = queue.finish(first.token, True, "plan-on")
    assert shown == "plan-on"
    assert following is None

    again = queue.request(True)
    assert again.cached == "plan-on"
    assert again.start is False


def test_only_one_worker_runs_and_the_newest_state_wins():
    queue: ReplanQueue[str] = ReplanQueue()
    first = queue.request(True)
    queued = queue.request(False)
    assert queued.start is False

    # The True result is stale, but still cached for a later toggle back.
    shown, following = queue.finish(first.token, True, "plan-on")
    assert shown is None
    assert following is not None and following.start is True
    assert following.state is False

    shown, following = queue.finish(following.token, False, "plan-off")
    assert shown == "plan-off"
    assert following is None
    assert queue.request(True).cached == "plan-on"


def test_a_burst_back_to_the_running_state_plans_once():
    queue: ReplanQueue[str] = ReplanQueue()
    first = queue.request(True)
    queue.request(False)
    queue.request(True)  # back to the state already being planned

    shown, following = queue.finish(first.token, True, "plan-on")
    assert shown == "plan-on"  # still the newest request
    assert following is None


def test_a_failure_does_not_retry_the_same_state_but_starts_a_newer_one():
    queue: ReplanQueue[str] = ReplanQueue()
    first = queue.request(True)
    assert queue.fail(first.token, True) is None  # no retry loop

    second = queue.request(True)  # an explicit toggle retries
    assert second.start is True
    queue.request(False)
    following = queue.fail(second.token, True)
    assert following is not None and following.state is False


def test_cached_results_are_the_exact_objects_planned():
    queue: ReplanQueue[object] = ReplanQueue()
    plan = object()
    first = queue.request(True)
    queue.finish(first.token, True, plan)
    assert queue.request(True).cached is plan
