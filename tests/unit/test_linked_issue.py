from app.utils.pr_checks import has_linked_issue


def test_closes_variants_match():
    assert has_linked_issue("Closes #12")
    assert has_linked_issue("Fixed #34")
    assert has_linked_issue("Resolve: #7")


def test_cross_repo_reference_matches():
    assert has_linked_issue("Fixes owner/repo#45")


def test_bare_number_without_keyword_does_not_match():
    assert not has_linked_issue("See #123 for context")
