import pytest
from fastapi import HTTPException

from app.utils.access import verify_repo_access


def test_allows_authorized_repo():
    verify_repo_access("hiero", "sdk", {"hiero"}, {"hiero": {"sdk"}})


def test_denies_other_repo_in_same_org():
    with pytest.raises(HTTPException) as exc:
        verify_repo_access("hiero", "secret", {"hiero"}, {"hiero": {"sdk"}})
    assert exc.value.status_code == 403


def test_legacy_org_without_repos_stays_open():
    verify_repo_access("hiero", "anything", {"hiero"}, {})


def test_no_repo_filter_is_allowed():
    verify_repo_access("hiero", None, {"hiero"}, {"hiero": {"sdk"}})
