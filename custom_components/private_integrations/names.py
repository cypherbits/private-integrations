"""Validation of integration folder names.

Home Assistant imports ``custom_components/<domain>`` and uses the folder name
as the integration domain, so the installer, the store and the discovery of
repositories all have to apply the same rule.
"""
from __future__ import annotations

import re

_INTEGRATION_NAME = re.compile(r"^[a-z0-9_]+$")


def is_valid_integration_name(value: object) -> bool:
    """True when ``value`` can be a ``custom_components`` folder name."""
    return isinstance(value, str) and _INTEGRATION_NAME.match(value) is not None
