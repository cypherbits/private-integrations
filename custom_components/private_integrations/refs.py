"""Parsing of user supplied repository references.

Kept free of Home Assistant imports so it can be unit tested on its own.
"""
from __future__ import annotations

import re

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_REPO = r"[A-Za-z0-9._-]+?"

# Matches "owner/repo", "github.com/owner/repo", "https://github.com/owner/repo[.git]"
# and "git@github.com:owner/repo[.git]", with or without a trailing slash.
_REPO_RE = re.compile(
    rf"^(?:git@github\.com:|(?:https?://)?(?:www\.)?github\.com/)?"
    rf"(?P<owner>{_OWNER})/(?P<repo>{_REPO})(?:\.git)?/?$"
)


def parse_repo_reference(value: str) -> str:
    """Normalise a repository reference to 'owner/repo'.

    Raises ValueError when the value is not a GitHub repository reference.
    """
    cleaned = value.strip()
    match = _REPO_RE.match(cleaned)
    if not match:
        raise ValueError(f"Could not parse repository reference: {value!r}")

    repo = match.group("repo").removesuffix(".git")
    return f"{match.group('owner')}/{repo}"
