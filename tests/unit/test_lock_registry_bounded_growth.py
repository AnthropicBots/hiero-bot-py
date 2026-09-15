from app.workflows.onboarding import (
    BoundedLockRegistry,
    _assign_locks,
    _contributor_assign_locks,
    _get_assign_lock,
    _get_contributor_assign_lock,
)


def test_module_level_registries_are_bounded_not_plain_dicts():
    assert isinstance(_assign_locks, BoundedLockRegistry)
    assert isinstance(_contributor_assign_locks, BoundedLockRegistry)
    assert _assign_locks._max_capacity < float("inf")
    assert _contributor_assign_locks._max_capacity < float("inf")


def test_per_issue_lock_registry_stays_bounded_across_many_issues():
    before = len(_assign_locks._locks)

    for issue_number in range(10_000):
        _get_assign_lock("hiero", "sdk-js", issue_number)

    assert len(_assign_locks._locks) <= _assign_locks._max_capacity
    assert len(_assign_locks._locks) < 10_000 + before


def test_per_contributor_lock_registry_stays_bounded_across_many_contributors():
    for i in range(10_000):
        _get_contributor_assign_lock("hiero", "sdk-js", f"contributor-{i}")

    assert len(_contributor_assign_locks._locks) <= _contributor_assign_locks._max_capacity


def test_assign_lock_is_reused_for_the_same_issue():
    first = _get_assign_lock("hiero", "sdk-js", 4242)
    second = _get_assign_lock("hiero", "sdk-js", 4242)

    assert first is second