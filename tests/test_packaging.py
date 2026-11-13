"""Consistency checks between the code, the strings and the service docs."""
import json
from pathlib import Path

from custom_components.private_integrations import const

COMPONENT_DIR = Path(const.__file__).parent
STRINGS = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))
EN_TRANSLATIONS = json.loads(
    (COMPONENT_DIR / "translations" / "en.json").read_text(encoding="utf-8")
)
ES_TRANSLATIONS = json.loads(
    (COMPONENT_DIR / "translations" / "es.json").read_text(encoding="utf-8")
)
SERVICES_YAML = (COMPONENT_DIR / "services.yaml").read_text(encoding="utf-8")

SERVICE_NAMES = {
    const.SERVICE_ADD_REPOSITORY,
    const.SERVICE_REMOVE_REPOSITORY,
    const.SERVICE_INSTALL,
    const.SERVICE_UPDATE,
}


def test_english_translations_match_the_source_strings():
    """``en.json`` is what Home Assistant reads; ``strings.json`` is the source.

    They are edited by hand here, so nobody regenerates the copy automatically
    and the UI would silently keep the old wording.
    """
    assert EN_TRANSLATIONS == STRINGS


def test_spanish_translation_is_complete():
    """Every key in the source strings exists in ``es.json`` too."""
    assert _key_tree(ES_TRANSLATIONS) == _key_tree(STRINGS)
    assert "" not in _leaf_values(ES_TRANSLATIONS)


def test_every_selector_option_is_translated():
    """Selector labels are rendered by the frontend from these keys."""
    ref_types = STRINGS["selector"]["ref_type"]["options"]
    assert set(ref_types) == set(const.VALID_REF_TYPES)

    actions = STRINGS["selector"]["repository_action"]["options"]
    assert set(actions) == {
        const.ACTION_INSTALL,
        const.ACTION_FORCE_INSTALL,
        const.ACTION_CHANGE_REF,
        const.ACTION_REMOVE,
    }

    for translation in (EN_TRANSLATIONS, ES_TRANSLATIONS):
        for selector in ("ref_type", "repository_action"):
            assert all(translation["selector"][selector]["options"].values())


def test_every_service_is_documented_and_translated():
    documented = set(_yaml_top_level_keys(SERVICES_YAML))
    assert documented == SERVICE_NAMES
    assert set(STRINGS["services"]) == SERVICE_NAMES
    for service, fields in _yaml_fields(SERVICES_YAML).items():
        assert set(STRINGS["services"][service]["fields"]) == set(fields)


def test_every_documented_service_field_is_accepted():
    """No field in services.yaml that the schema would reject."""
    expected = {
        const.SERVICE_ADD_REPOSITORY: {
            const.CONF_FULL_NAME,
            const.CONF_REF_TYPE,
            const.CONF_REF,
            const.CONF_INTEGRATION_NAME,
        },
        const.SERVICE_REMOVE_REPOSITORY: {
            const.CONF_FULL_NAME,
            const.CONF_DELETE_FILES,
        },
        const.SERVICE_INSTALL: {const.CONF_FULL_NAME, const.CONF_FORCE},
        const.SERVICE_UPDATE: {const.CONF_FULL_NAME, const.CONF_FORCE},
    }
    for service, fields in _yaml_fields(SERVICES_YAML).items():
        assert set(fields) == expected[service]


def test_every_options_step_exists_in_the_flow():
    """A step in strings.json that the flow does not implement is dead UI."""
    from custom_components.private_integrations.config_flow import (
        PrivateIntegrationsOptionsFlow,
    )

    for step_id in STRINGS["options"]["step"]:
        if step_id == "init":
            continue
        assert hasattr(
            PrivateIntegrationsOptionsFlow, f"async_step_{step_id}"
        ), f"strings.json documents a missing step: {step_id}"


def test_the_detail_is_not_printed_twice():
    """Errors already carry the detail; repeating it in the body is noise."""
    for step in STRINGS["options"]["step"].values():
        assert "{detail}" not in step.get("description", "")
    for error in STRINGS["options"]["error"].values():
        assert "{detail}" in error or "detail" not in error


def _key_tree(value):
    """Return the shape of a translation tree, ignoring the translated text."""
    if isinstance(value, dict):
        return {key: _key_tree(item) for key, item in value.items()}
    return None


def _leaf_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaf_values(item)
    else:
        yield value


def _yaml_top_level_keys(text: str) -> list[str]:
    return [line.split(":")[0] for line in text.splitlines() if line[:1] not in " "]


def _yaml_fields(text: str) -> dict[str, list[str]]:
    """Return {service: [field names]} for the small services.yaml above."""
    fields: dict[str, list[str]] = {}
    service = None
    in_fields = False
    for line in text.splitlines():
        if line and not line.startswith(" "):
            service = line.split(":")[0]
            fields[service] = []
            in_fields = False
        elif line.strip() == "fields:":
            in_fields = True
        elif in_fields and line.startswith("    ") and not line.startswith("      "):
            fields[service].append(line.strip().rstrip(":"))
    return fields
