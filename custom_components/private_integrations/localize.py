"""Translate the few UI strings that are composed in Python.

Home Assistant translates titles, labels and descriptions in the frontend, so
those live in ``strings.json``/``translations``. A notice or a notification is
built by the integration itself, though, and needs the text up front; that is
what the ``common`` section of the translation files is for.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN


async def async_translate(
    hass: HomeAssistant, key: str, **placeholders: str
) -> str:
    """Return ``common.<key>`` in the current language.

    English is tried next, so an incomplete translation degrades to readable
    text instead of a raw key. If neither file defines the key, the key itself
    is returned.
    """
    languages = dict.fromkeys((hass.config.language, "en"))
    for language in languages:
        translations = await async_get_translations(
            hass, language, "common", [DOMAIN]
        )
        template = translations.get(f"component.{DOMAIN}.common.{key}")
        if template is not None:
            return template.format(**placeholders)
    return key
