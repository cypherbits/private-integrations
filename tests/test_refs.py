"""Tests for repository reference parsing."""
import pytest

from custom_components.private_integrations.refs import parse_repo_reference


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("owner/repo", "owner/repo"),
        (" owner/repo ", "owner/repo"),
        ("Owner/Repo.Name", "Owner/Repo.Name"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("http://www.github.com/owner/repo/", "owner/repo"),
        ("github.com/owner/repo", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo", "owner/repo"),
        ("owner/repo.git.git", "owner/repo"),
    ],
)
def test_parse_repo_reference(value, expected):
    assert parse_repo_reference(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "owner",
        "owner/repo/tree/main",
        "https://gitlab.com/owner/repo",
        "owner/re po",
        "-bad-/repo",
        "owner/repo?tab=readme",
    ],
)
def test_parse_repo_reference_rejects(value):
    with pytest.raises(ValueError):
        parse_repo_reference(value)
