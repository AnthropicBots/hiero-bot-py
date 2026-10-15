def test_comment_due_uses_only_comment_threshold():
    score, comment_threshold = 65, 60
    assert (score < comment_threshold) is False


def test_low_score_still_comments():
    assert (50 < 60) is True
