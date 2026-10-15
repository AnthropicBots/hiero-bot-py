from app.workflows.pullrequest import _score_candidates


def test_author_excluded_case_insensitively():
    histories = [[{"sha": "a", "author": {"login": "alice"}}]]
    assert _score_candidates(histories, exclude="Alice") == {}


def test_other_contributor_still_scores():
    histories = [[{"sha": "b", "author": {"login": "bob"}}]]
    assert _score_candidates(histories, exclude="Alice") == {"bob": 1}
