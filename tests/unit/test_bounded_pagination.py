import inspect

from app.github.client import GitHubClient


def test_comment_and_file_listing_use_bounded_paginate():
    assert "self.paginate" in inspect.getsource(GitHubClient.list_issue_comments)
    assert "while True" not in inspect.getsource(GitHubClient.list_issue_comments)
    assert "self.paginate" in inspect.getsource(GitHubClient.list_pr_files)
    assert "while True" not in inspect.getsource(GitHubClient.list_pr_files)


def test_commit_listing_paginates():
    assert "self.paginate" in inspect.getsource(GitHubClient.list_pr_commits)
