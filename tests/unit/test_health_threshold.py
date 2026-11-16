import pytest

from app.workflows.prhealth import (
    HEALTH_MARKER,
    LABEL_HEALTHY,
    LABEL_NEEDS_WORK,
    PRHealthWorkflow,
)


def make_payload():
    return {
        "pull_request": {
            "number": 901,
            "title": "feat: add feature",
            "body": "Closes #10",
            "user": {"login": "alice"},
            "head": {"sha": "abc123"},
            "additions": 10,
            "deletions": 5,
        }
    }


@pytest.mark.asyncio
async def test_healthy_label_can_coexist_with_comment_when_comment_threshold_is_higher(
    mock_gh, ctx, monkeypatch
):
    """A score can be healthy by label threshold but still require a comment."""
    cfg = ctx["config"].workflows.pr_health
    cfg.comment_threshold = 85
    cfg.label_healthy_above = 75

    monkeypatch.setattr(
        PRHealthWorkflow,
        "_compute_score",
        staticmethod(lambda signals, weights: 80.0),
    )

    wf = PRHealthWorkflow(mock_gh)
    await wf.score_pr(ctx, make_payload())

    assert mock_gh.add_label.call_args.args[3] == LABEL_HEALTHY
    mock_gh.post_comment.assert_awaited_once()

    comment_body = mock_gh.post_comment.call_args.args[3]
    assert HEALTH_MARKER in comment_body
    assert "85/100" in comment_body
    assert "comment threshold" in comment_body


@pytest.mark.asyncio
async def test_score_equal_to_comment_threshold_does_not_post_comment(
    mock_gh, ctx, monkeypatch
):
    """The comment condition is strictly score < comment_threshold."""
    cfg = ctx["config"].workflows.pr_health
    cfg.comment_threshold = 60
    cfg.label_healthy_above = 75

    monkeypatch.setattr(
        PRHealthWorkflow,
        "_compute_score",
        staticmethod(lambda signals, weights: 60.0),
    )

    wf = PRHealthWorkflow(mock_gh)
    await wf.score_pr(ctx, make_payload())

    assert mock_gh.add_label.call_args.args[3] == LABEL_NEEDS_WORK
    mock_gh.post_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_updates_existing_health_comment_without_duplicate(
    mock_gh, ctx, monkeypatch
):
    """Crossing the comment threshold updates the existing warning comment."""
    cfg = ctx["config"].workflows.pr_health
    cfg.comment_threshold = 60
    cfg.label_healthy_above = 75

    scores = iter((50.0, 70.0))
    monkeypatch.setattr(
        PRHealthWorkflow,
        "_compute_score",
        staticmethod(lambda signals, weights: next(scores)),
    )

    existing_comment = {
        "id": 123,
        "body": f"{HEALTH_MARKER}\nPrevious health warning",
        "user": {"type": "Bot"},
    }
    mock_gh.list_issue_comments.side_effect = [
        [],
        [existing_comment],
    ]

    wf = PRHealthWorkflow(mock_gh)

    # First scoring: below the comment threshold, so a warning is posted.
    await wf.score_pr(ctx, make_payload())
    mock_gh.post_comment.assert_awaited_once()

    # Second scoring: above the comment threshold, but below the label
    # threshold. The existing warning should be edited, while the label
    # should remain needs-work.
    await wf.score_pr(ctx, make_payload())

    assert mock_gh.add_label.call_args.args[3] == LABEL_NEEDS_WORK

    mock_gh.update_comment.assert_awaited_once()
    update_args = mock_gh.update_comment.call_args.args

    assert update_args[2] == 123
    assert HEALTH_MARKER in update_args[3]
    assert "70/100" in update_args[3]
    assert "60/100" in update_args[3]
    assert "reached or exceeded" in update_args[3]
    assert "comment threshold" in update_args[3]

    # Only the original warning should have been posted.
    mock_gh.post_comment.assert_awaited_once()
