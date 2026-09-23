#!/usr/bin/env python3
"""Ruleset for Hetzner Cloud Load Balancer monitoring."""

from __future__ import annotations

from cmk.rulesets.v1 import Help, Label, Message, Title
from cmk.rulesets.v1.form_specs import (
    BooleanChoice,
    CascadingSingleChoice,
    CascadingSingleChoiceElement,
    DefaultValue,
    DictElement,
    Dictionary,
    FixedValue,
    Integer,
    Password,
    String,
    migrate_to_password,
    validators,
)
from cmk.rulesets.v1.rule_specs import SpecialAgent, Topic

DEFAULT_API_URL = "https://api.hetzner.cloud/v1"
DEFAULT_CACHE_TTL_SECONDS = 60
DEFAULT_CACHE_MAX_STALE_AGE_SECONDS = 900


def _cache_ttl_value(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return DEFAULT_CACHE_TTL_SECONDS
    if isinstance(value, (int, float)):
        return max(1, int(value))
    if isinstance(value, str):
        try:
            return max(1, int(float(value.strip())))
        except ValueError:
            return DEFAULT_CACHE_TTL_SECONDS
    return DEFAULT_CACHE_TTL_SECONDS


def _cache_max_stale_age_value(value: object, cache_ttl: int) -> int:
    if value is None or isinstance(value, bool):
        return max(DEFAULT_CACHE_MAX_STALE_AGE_SECONDS, cache_ttl)
    if isinstance(value, (int, float)):
        return max(1, int(value))
    if isinstance(value, str):
        try:
            return max(1, int(float(value.strip())))
        except ValueError:
            return max(DEFAULT_CACHE_MAX_STALE_AGE_SECONDS, cache_ttl)
    return max(DEFAULT_CACHE_MAX_STALE_AGE_SECONDS, cache_ttl)


def _cache_bool_value(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return bool(value)


def _migrate_cache_enabled(
    value: object,
    legacy_ttl: object = None,
    legacy_stale_on_error: object = None,
    legacy_max_stale_age: object = None,
) -> tuple[str, dict[str, object] | None]:
    """Normalize cache values into the CascadingSingleChoice tuple shape."""
    stale_on_error = _cache_bool_value(legacy_stale_on_error, True)

    if isinstance(value, tuple) and len(value) == 2:
        choice, nested_value = value
        if _cache_bool_value(choice, True) is False:
            return ("disabled", None)
        if not isinstance(nested_value, dict):
            nested_value = {}
        cache_ttl = _cache_ttl_value(
            nested_value.get("cache_ttl", nested_value.get("ttl", legacy_ttl))
        )
        return (
            "enabled",
            {
                "cache_ttl": cache_ttl,
                "cache_max_stale_age": _cache_max_stale_age_value(
                    nested_value.get(
                        "cache_max_stale_age",
                        nested_value.get("max_stale_age", legacy_max_stale_age),
                    ),
                    cache_ttl,
                ),
                "cache_stale_on_error": _cache_bool_value(
                    nested_value.get(
                        "cache_stale_on_error",
                        nested_value.get("stale_on_error", stale_on_error),
                    ),
                    True,
                ),
            },
        )

    if not isinstance(value, dict) and _cache_bool_value(value, True) is False:
        return ("disabled", None)

    if isinstance(value, dict):
        enabled = value.get("enabled", value.get("cache_enabled"))
        if enabled is not None and _cache_bool_value(enabled, True) is False:
            return ("disabled", None)
        cache_ttl = _cache_ttl_value(value.get("cache_ttl", value.get("ttl", legacy_ttl)))
        return (
            "enabled",
            {
                "cache_ttl": cache_ttl,
                "cache_max_stale_age": _cache_max_stale_age_value(
                    value.get(
                        "cache_max_stale_age",
                        value.get("max_stale_age", legacy_max_stale_age),
                    ),
                    cache_ttl,
                ),
                "cache_stale_on_error": _cache_bool_value(
                    value.get("cache_stale_on_error", value.get("stale_on_error", stale_on_error)),
                    True,
                ),
            },
        )

    cache_ttl = _cache_ttl_value(legacy_ttl)
    return (
        "enabled",
        {
            "cache_ttl": cache_ttl,
            "cache_max_stale_age": _cache_max_stale_age_value(legacy_max_stale_age, cache_ttl),
            "cache_stale_on_error": stale_on_error,
        },
    )


def _validate_cache_settings(value: object) -> None:
    if not isinstance(value, dict):
        return
    cache_ttl = _cache_ttl_value(value.get("cache_ttl"))
    max_stale_age = _cache_max_stale_age_value(value.get("cache_max_stale_age"), cache_ttl)
    stale_on_error = _cache_bool_value(value.get("cache_stale_on_error"), True)
    if stale_on_error and max_stale_age < cache_ttl:
        raise validators.ValidationError(
            Message("Maximum stale age must be at least as large as the result cache TTL.")
        )


def _migrate_integration_params(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {
            "fetch_metrics": True,
            "cache_enabled": _migrate_cache_enabled(True),
        }

    migrated = dict(value)
    migrated.setdefault("fetch_metrics", True)
    legacy_cache_group = migrated.pop("cache", None)
    legacy_ttl = migrated.pop("cache_ttl", None)
    legacy_stale_on_error = migrated.pop("cache_stale_on_error", None)
    legacy_max_stale_age = migrated.pop("cache_max_stale_age", migrated.pop("max_stale_age", None))
    migrated["cache_enabled"] = _migrate_cache_enabled(
        migrated.get("cache_enabled", legacy_cache_group if legacy_cache_group is not None else True),
        legacy_ttl,
        legacy_stale_on_error,
        legacy_max_stale_age,
    )
    return migrated


def _result_cache_form() -> CascadingSingleChoice:
    return CascadingSingleChoice(
        title=Title("Result cache"),
        help_text=Help(
            "Cache normalized special-agent results to reduce repeated Hetzner Cloud API calls. "
            "The short default TTL keeps operational Load Balancer health and metrics current."
        ),
        migrate=_migrate_cache_enabled,
        prefill=DefaultValue("enabled"),
        elements=(
            CascadingSingleChoiceElement(
                name="disabled",
                title=Title("Disabled"),
                parameter_form=FixedValue(value=None),
            ),
            CascadingSingleChoiceElement(
                name="enabled",
                title=Title("Enabled"),
                parameter_form=Dictionary(
                    custom_validate=(_validate_cache_settings,),
                    elements={
                        "cache_ttl": DictElement(
                            required=True,
                            parameter_form=Integer(
                                title=Title("Result cache TTL"),
                                help_text=Help("Cache validity in seconds. Default: 60 seconds."),
                                unit_symbol="seconds",
                                prefill=DefaultValue(DEFAULT_CACHE_TTL_SECONDS),
                                custom_validate=(validators.NumberInRange(min_value=1),),
                            ),
                        ),
                        "cache_max_stale_age": DictElement(
                            required=True,
                            parameter_form=Integer(
                                title=Title("Maximum stale age"),
                                help_text=Help(
                                    "Absolute maximum age in seconds for stale fallback. Default: "
                                    "900 seconds. Older entries are never used after a refresh failure."
                                ),
                                unit_symbol="seconds",
                                prefill=DefaultValue(DEFAULT_CACHE_MAX_STALE_AGE_SECONDS),
                                custom_validate=(validators.NumberInRange(min_value=1),),
                            ),
                        ),
                        "cache_stale_on_error": DictElement(
                            required=True,
                            parameter_form=BooleanChoice(
                                title=Title("Use stale cache on collection error"),
                                help_text=Help(
                                    "If collection fails after the cache expires, use the last cached "
                                    "result and visibly report it as stale instead of hiding the failure."
                                ),
                                prefill=DefaultValue(True),
                            ),
                        ),
                    },
                ),
            ),
        ),
    )


def _special_agent_parameter_form() -> Dictionary:
    return Dictionary(
        title=Title("Hetzner Cloud Load Balancers"),
        migrate=_migrate_integration_params,
        help_text=Help(
            "Monitor all Load Balancers in a Hetzner Cloud project with a read-only Cloud API token. "
            "Target health is Hetzner's backend health assessment and does not replace an external "
            "availability check."
        ),
        elements={
            "api_token": DictElement(
                required=True,
                parameter_form=Password(
                    title=Title("API token"),
                    help_text=Help("Read-only bearer token for the Hetzner Cloud API."),
                    migrate=migrate_to_password,
                ),
            ),
            "api_url": DictElement(
                required=False,
                parameter_form=String(
                    title=Title("API base URL"),
                    help_text=Help("Override only for a compatible Hetzner Cloud API endpoint."),
                    prefill=DefaultValue(DEFAULT_API_URL),
                    custom_validate=(
                        validators.Url(protocols=(validators.UrlProtocol.HTTP, validators.UrlProtocol.HTTPS)),
                    ),
                ),
            ),
            "timeout": DictElement(
                required=False,
                parameter_form=Integer(
                    title=Title("API timeout"),
                    unit_symbol="s",
                    prefill=DefaultValue(10),
                    custom_validate=(validators.NumberInRange(min_value=1),),
                ),
            ),
            "fetch_metrics": DictElement(
                required=True,
                parameter_form=BooleanChoice(
                    label=Label("Fetch performance metrics"),
                    help_text=Help(
                        "Query the metrics endpoint for every Load Balancer. Disable this to monitor "
                        "configuration and target health without performance services."
                    ),
                    prefill=DefaultValue(True),
                ),
            ),
            "cache_enabled": DictElement(
                required=False,
                parameter_form=_result_cache_form(),
            ),
        },
        ignored_elements=(
            "cache",
            "cache_ttl",
            "cache_stale_on_error",
            "cache_max_stale_age",
            "result_cache",
        ),
    )


rule_spec_hetzner_loadbalancer = SpecialAgent(
    name="hetzner_loadbalancer",
    title=Title("Hetzner Cloud Load Balancers"),
    topic=Topic.APPLICATIONS,
    parameter_form=_special_agent_parameter_form,
)
