"""Repository data model."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .const import REF_TYPE_RELEASE, VALID_REF_TYPES
from .names import is_valid_integration_name
from .refs import parse_repo_reference

#: Fields that hold a plain string (or nothing) in the stored representation.
_TEXT_FIELDS = ("ref", "integration_name", "installed_version", "installed_sha")


@dataclass
class Repository:
    """A tracked GitHub repository providing a custom integration."""

    full_name: str
    ref_type: str = REF_TYPE_RELEASE
    ref: str | None = None
    integration_name: str | None = None
    installed_version: str | None = None
    installed_sha: str | None = None
    adopt: bool = False

    @property
    def key(self) -> str:
        return self.full_name.lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Repository:
        """Build a repository from its stored form, rejecting broken values.

        The storage file can be edited by hand and is not created by this class
        alone. A wrong type used to be accepted and then blow up somewhere far
        from the real cause -- an integer ``full_name`` raised AttributeError
        while the store built its lookup key, i.e. during setup -- so anything
        unreadable is rejected here, where the caller can skip the entry and say
        which one it was.
        """
        allowed = {f for f in cls.__dataclass_fields__}
        values = {k: v for k, v in data.items() if k in allowed}
        integration_name = _optional_text(values, "integration_name")
        if integration_name is not None:
            integration_name = integration_name.strip() or None
        if integration_name is not None and not is_valid_integration_name(
            integration_name
        ):
            raise ValueError(
                "integration_name is not a folder name: "
                f"{integration_name!r}"
            )
        return cls(
            full_name=_owner_slash_repo(_required_text(values, "full_name")),
            ref_type=_ref_type(values.get("ref_type", REF_TYPE_RELEASE)),
            ref=_optional_text(values, "ref"),
            integration_name=integration_name,
            installed_version=_optional_text(values, "installed_version"),
            installed_sha=_optional_text(values, "installed_sha"),
            adopt=_optional_bool(values, "adopt"),
        )


def _owner_slash_repo(full_name: str) -> str:
    """Reject a stored name that is not already ``owner/repo``.

    Every API call builds its URL from this value, so a hand-edited entry such as
    ``"owner/repo?x=1"`` would change the request. Entries are always written
    normalised, which makes anything else a sign of a manual edit.
    """
    try:
        normalised = parse_repo_reference(full_name)
    except ValueError as err:
        raise ValueError(
            f"full_name is not 'owner/repo': {full_name!r}"
        ) from err
    if normalised != full_name:
        raise ValueError(
            f"full_name should be stored as {normalised!r}, not {full_name!r}"
        )
    return full_name


def _required_text(values: dict[str, Any], key: str) -> str:
    value = _optional_text(values, key)
    if value is None:
        raise ValueError(f"{key} is missing")
    if not value.strip():
        raise ValueError(f"{key} is empty")
    return value


def _optional_text(values: dict[str, Any], key: str) -> str | None:
    """Return a text field, or None when it is absent."""
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, not {type(value).__name__}")
    return value


def _optional_bool(values: dict[str, Any], key: str) -> bool:
    """Return a boolean field, defaulting to False when it is absent."""
    value = values.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean, not {type(value).__name__}")
    return value


def _ref_type(value: Any) -> str:
    if not isinstance(value, str) or value not in VALID_REF_TYPES:
        raise ValueError(
            f"ref_type must be one of {list(VALID_REF_TYPES)}, not {value!r}"
        )
    return value
