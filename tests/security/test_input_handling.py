# tests/security/test_input_handling.py — untrusted input handling (#20)

import base64

import pytest
import yaml
from pydantic import ValidationError

from app.config.schema import RepoConfig
from app.github.client import _normalize_private_key
from app.utils import audit

# ── Repository config is attacker-controlled YAML ─────────────


def test_yaml_object_construction_is_refused():
    """
    Anyone who can push to a repo writes .github/hiero-bot.yml. If the loader
    used yaml.load, that file would be remote code execution on the bot host.
    """
    malicious = "!!python/object/apply:os.system ['echo pwned']"

    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(malicious)


def test_yaml_name_tags_are_refused():
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load("!!python/name:os.system {}")


def test_yaml_aliases_do_not_bypass_config_schema():
    """YAML aliases must not introduce data outside the expected schema."""
    bomb = """
    a: &a ["x", "x", "x"]
    b: &b [*a, *a, *a]
    repo: "hiero/sdk-js"
    workflows:
      pr_health: *b
    """

    data = yaml.safe_load(bomb)

    with pytest.raises(ValidationError):
        RepoConfig.model_validate(data)


@pytest.mark.parametrize(
    "repo",
    [
        "../../etc/passwd",
        "owner/repo/../../other",
        "owner/repo; rm -rf /",
        "owner",
        "/absolute/path",
        "owner/repo\nsecond: line",
        "",
    ],
)
def test_repo_slug_rejects_traversal_and_injection(repo):
    with pytest.raises(ValidationError):
        RepoConfig.model_validate({"repo": repo, "workflows": {}})


def test_valid_repo_slugs_are_accepted():
    for slug in ["hiero/sdk-js", "a/b", "Org.Name/repo_name-1"]:
        assert (
            RepoConfig.model_validate(
                {"repo": slug, "workflows": {}}
            ).repo
            == slug
        )


@pytest.mark.parametrize(
    "workflows",
    [
        {"pr_health": {"comment_threshold": 10_000}},
        {"pr_health": {"label_healthy_above": -1}},
        {"onboarding": {"minimum_account_age_days": -5}},
        {"pull_request": {"ai_review": {"max_comments": 9999}}},
        {"pull_request": {"quality_gates": {"max_files_changed": 0}}},
        {"issue_management": {"stale_issue_days": 0}},
    ],
)
def test_out_of_range_values_are_rejected(workflows):
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/sdk-js",
                "workflows": workflows,
            }
        )


def test_contradictory_stale_windows_are_rejected():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/sdk-js",
                "workflows": {
                    "issue_management": {
                        "stale_issue_days": 5,
                        "close_stale_after_days": 10,
                    }
                },
            }
        )


def test_unknown_focus_area_is_rejected():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(
            {
                "repo": "hiero/sdk-js",
                "workflows": {
                    "pull_request": {
                        "ai_review": {
                            "focus_areas": ["rm -rf"],
                        }
                    }
                },
            }
        )


# ── Audit trail integrity ─────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_audit_actions_are_refused(db):
    """The audit log is the record of what the bot did; its vocabulary is fixed."""
    with pytest.raises(ValueError, match="Unknown audit action"):
        await audit.record(
            db,
            action="totally.made.up",
            owner="hiero",
            repo="sdk-js",
            reason="should not be written",
        )


@pytest.mark.asyncio
async def test_audit_entries_record_actor_and_reason(db):
    entry = await audit.record(
        db,
        action="issue.assigned",
        owner="hiero",
        repo="sdk-js",
        target_login="alice",
        target_number=1,
        reason="Self-assignment via /assign",
    )

    assert entry.actor == "hiero-bot"
    assert entry.reason == "Self-assignment via /assign"


@pytest.mark.asyncio
async def test_audit_metadata_round_trips_untrusted_strings(db):
    """Untrusted metadata is stored as data rather than interpreted as commands."""
    hostile = {
        "label": "<script>alert(1)</script>",
        "team": "'; DROP TABLE x;--",
    }

    entry = await audit.record(
        db,
        action="issue.labeled",
        owner="hiero",
        repo="sdk-js",
        reason="escalation",
        metadata=hostile,
    )

    assert entry.metadata_json == hostile


# ── Private key handling ──────────────────────────────────────


def test_flattened_pem_is_normalised():
    key = (
        "-----BEGIN RSA PRIVATE KEY-----"
        "MIIBOgIBAAJBAK"
        "-----END RSA PRIVATE KEY-----"
    )

    normalised = _normalize_private_key(key)

    assert normalised.startswith(
        "-----BEGIN RSA PRIVATE KEY-----\n"
    )
    assert normalised.endswith(
        "\n-----END RSA PRIVATE KEY-----"
    )


def test_escaped_newlines_are_expanded():
    key = (
        "-----BEGIN PRIVATE KEY-----\\n"
        "BODY\\n"
        "-----END PRIVATE KEY-----"
    )

    normalised = _normalize_private_key(key)

    assert normalised == (
        "-----BEGIN PRIVATE KEY-----\n"
        "BODY\n"
        "-----END PRIVATE KEY-----"
    )


@pytest.mark.parametrize(
    "key",
    [
        "",
        "not a key at all",
        "-----BEGIN RSA PRIVATE KEY-----",
        base64.b64encode(b"x").decode(),
    ],
)
def test_malformed_private_keys_are_refused(key):
    with pytest.raises(RuntimeError):
        _normalize_private_key(key)


def test_unsupported_pem_type_is_refused():
    key = (
        "-----BEGIN CERTIFICATE-----"
        "BODY"
        "-----END CERTIFICATE-----"
    )

    with pytest.raises(RuntimeError, match="Unsupported"):
        _normalize_private_key(key)
