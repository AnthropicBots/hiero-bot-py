import re

PATTERN = re.compile(r"(?:^|/)(CHANGELOG|CHANGES|HISTORY)", re.IGNORECASE)


def test_root_changelog_matches():
    assert PATTERN.search("CHANGELOG.md")


def test_nested_changelog_matches():
    assert PATTERN.search("docs/CHANGELOG.md")
    assert PATTERN.search("packages/foo/CHANGES.md")


def test_non_changelog_does_not_match():
    assert not PATTERN.search("src/app.py")
