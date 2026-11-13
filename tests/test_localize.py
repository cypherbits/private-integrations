"""Runtime translation of the strings the integration composes in Python."""
from __future__ import annotations

from custom_components.private_integrations.localize import async_translate


async def test_english_is_used_by_default(hass):
    assert await async_translate(hass, "files_kept") == "were left on disk"


async def test_spanish_is_used_when_configured(hass):
    hass.config.language = "es"
    assert await async_translate(hass, "files_kept") == "se dejaron en disco"


async def test_a_missing_key_falls_back_to_the_key(hass):
    assert await async_translate(hass, "missing_key") == "missing_key"
