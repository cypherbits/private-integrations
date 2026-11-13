"""Translate the few UI strings that are composed in Python.

Home Assistant translates titles, labels and descriptions in the frontend, so
those live in ``strings.json``/``translations``. A notice or a notification is
built by the integration itself, though, and needs the text up front; that is
what the ``common`` section of the translation files is for.
"""
from __future__ import annotations

from collections import defaultdict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN


def _format_template(template: str, placeholders: dict[str, str]) -> str:
    """Fill a translation template without letting a typo escape as a crash.

    A missing placeholder becomes an empty string and a malformed template is
    returned as it is: an install has already written its files by the time the
    notification is built, so this must never fail afterwards.
    """
    try:
        return template.format_map(defaultdict(str, placeholders))
    except (KeyError, IndexError, ValueError):
        return template


def _languages(language: str) -> list[str]:
    """Languages to try, from the most specific to English.

    Home Assistant only falls back to English on its own, so ``es-419`` would
    otherwise ignore ``es.json``. The base language is inserted here.
    """
    candidates = [language]
    if "-" in language:
        candidates.append(language.split("-", 1)[0])
    candidates.append("en")
    return list(dict.fromkeys(candidates))


async def async_translate(
    hass: HomeAssistant, key: str, **placeholders: str
) -> str:
    """Return ``common.<key>`` in the current language.

    English is tried next, so an incomplete translation degrades to readable
    text instead of a raw key. If neither file defines the key, the key itself
    is returned.
    """
    for language in _languages(hass.config.language):
        translations = await async_get_translations(
            hass, language, "common", [DOMAIN]
        )
        template = translations.get(f"component.{DOMAIN}.common.{key}")
        if template is not None:
            return _format_template(template, placeholders)
    return key
