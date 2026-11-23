import pytest

from app.utils.pr_checks import has_linked_issue


@pytest.mark.parametrize(
    "reference",
    [
        "Resolve #123",
        "Resolves #123",
        "Fix #123",
        "Fixes: #123",
        "Closes #123",
        "Close #123",
    ],
)
def test_closes_variants_match(reference):
    assert has_linked_issue(reference)


@pytest.mark.parametrize(
    "body",
    [
        "#123",
        "See #123 for context",
        "This PR mentions issue #123",
        "Fixed #34",
        "Resolve: #7",
        "Fixes owner/repo#45",
    ],
)
def test_unsupported_references_do_not_match(body):
    assert not has_linked_issue(body)


def test_linked_issue_matching_is_case_insensitive():
    assert has_linked_issue("RESOLVES #123")
    assert has_linked_issue("fIxEs: #123")
