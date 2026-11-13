"""Runtime translation of the strings the integration composes in Python."""
from __future__ import annotations

from custom_components.private_integrations.localize import (
    _format_template,
    async_translate,
)


async def test_english_is_used_by_default(hass):
    assert await async_translate(hass, "files_kept") == "were left on disk"


async def test_spanish_is_used_when_configured(hass):
    hass.config.language = "es"
    assert await async_translate(hass, "files_kept") == "se dejaron en disco"


async def test_latin_american_spanish_uses_the_spanish_file(hass):
    """Home Assistant has no es-419 -> es fallback of its own."""
    hass.config.language = "es-419"
    assert await async_translate(hass, "files_kept") == "se dejaron en disco"


async def test_a_missing_key_falls_back_to_the_key(hass):
    assert await async_translate(hass, "missing_key") == "missing_key"


def test_a_missing_placeholder_does_not_crash():
    assert _format_template("{repo} {missing}", {"repo": "x"}) == "x "


def test_a_malformed_template_is_returned_as_is():
    assert _format_template("{broken", {}) == "{broken"
