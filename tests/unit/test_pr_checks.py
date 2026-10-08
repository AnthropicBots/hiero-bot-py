# tests/unit/test_pr_checks.py — shared helpers used by both PR workflows (issue #123)

import pytest

from app.utils.pr_checks import commits_have_signoff, is_test_file


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("tests/unit/test_foo.py", True),
        ("test/foo.js", True),
        ("app/foo_test.py", True),
        ("test_foo.py", True),
        ("web/src/a.test.ts", True),
        ("web/src/a.spec.tsx", True),
        ("web/__tests__/a.js", True),
        # false positives the unanchored regexes used to accept
        ("contest/app.py", False),
        ("contest_utils.py", False),
        ("src/latest/handler.py", False),
        ("app/attest.py", False),
        ("app/main.py", False),
    ],
)
def test_is_test_file(filename, expected):
    assert is_test_file(filename) is expected


def _commit(message, parents=1):
    return {"commit": {"message": message}, "parents": [{}] * parents}


def test_signoff_present():
    assert commits_have_signoff(
        [_commit("fix\n\nSigned-off-by: Alice <alice@example.com>")]
    )


def test_signoff_is_case_insensitive():
    assert commits_have_signoff(
        [_commit("fix\n\nsigned-off-by: Alice <alice@example.com>")]
    )


def test_missing_signoff_on_any_commit_fails():
    assert not commits_have_signoff(
        [_commit("a\n\nSigned-off-by: A <a@b.co>"), _commit("b")]
    )


def test_merge_commits_are_ignored():
    assert commits_have_signoff(
        [
            _commit("a\n\nSigned-off-by: A <a@b.co>"),
            _commit("Merge branch 'main'", parents=2),
        ]
    )


@pytest.mark.parametrize("commits", [[], [_commit("Merge x", parents=2)]])
def test_no_non_merge_commits_is_not_a_pass(commits):
    assert commits_have_signoff(commits) is False
