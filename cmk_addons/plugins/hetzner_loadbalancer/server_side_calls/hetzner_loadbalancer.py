#!/usr/bin/env python3
"""Server-side call for the Hetzner Cloud Load Balancer special agent."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cmk.server_side_calls.v1 import Secret, SpecialAgentCommand, SpecialAgentConfig, noop_parser

DEFAULT_API_URL = "https://api.hetzner.cloud/v1"
DEFAULT_CACHE_TTL_SECONDS = 60
DEFAULT_CACHE_MAX_STALE_AGE_SECONDS = 900


def _bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _first_present(params: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in params and params[key] is not None:
            return params[key]
    return None


def _int_value(value: Any, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip())))
        except ValueError:
            return default
    return default


def _cache_arguments(params: Mapping[str, Any]) -> tuple[bool, int, int, bool]:
    raw_cache = _first_present(params, ("cache_enabled", "cache", "result_cache"))
    ttl_value = _first_present(params, ("cache_ttl",))
    max_stale_age_value = _first_present(params, ("cache_max_stale_age", "max_stale_age"))
    stale_on_error_value = _first_present(params, ("cache_stale_on_error", "stale_on_error"))

    if isinstance(raw_cache, tuple) and len(raw_cache) == 2:
        choice, nested_value = raw_cache
        cache_enabled = _bool_value(choice, True)
        if isinstance(nested_value, Mapping):
            nested_ttl = _first_present(nested_value, ("cache_ttl", "ttl"))
            if nested_ttl is not None:
                ttl_value = nested_ttl
            nested_max_stale_age = _first_present(
                nested_value, ("cache_max_stale_age", "max_stale_age")
            )
            if nested_max_stale_age is not None:
                max_stale_age_value = nested_max_stale_age
            nested_stale_on_error = _first_present(
                nested_value, ("cache_stale_on_error", "stale_on_error")
            )
            if nested_stale_on_error is not None:
                stale_on_error_value = nested_stale_on_error
    elif isinstance(raw_cache, Mapping):
        cache_enabled = _bool_value(_first_present(raw_cache, ("enabled", "cache_enabled")), True)
        nested_ttl = _first_present(raw_cache, ("cache_ttl", "ttl"))
        if nested_ttl is not None:
            ttl_value = nested_ttl
        nested_max_stale_age = _first_present(raw_cache, ("cache_max_stale_age", "max_stale_age"))
        if nested_max_stale_age is not None:
            max_stale_age_value = nested_max_stale_age
        nested_stale_on_error = _first_present(
            raw_cache, ("cache_stale_on_error", "stale_on_error")
        )
        if nested_stale_on_error is not None:
            stale_on_error_value = nested_stale_on_error
    else:
        cache_enabled = _bool_value(raw_cache, True)

    cache_ttl = _int_value(ttl_value, DEFAULT_CACHE_TTL_SECONDS)
    max_stale_age = _int_value(
        max_stale_age_value,
        max(DEFAULT_CACHE_MAX_STALE_AGE_SECONDS, cache_ttl),
    )
    stale_on_error = _bool_value(stale_on_error_value, True)
    if cache_enabled and cache_ttl < 1:
        raise ValueError("Result cache TTL must be at least one second")
    if cache_enabled and max_stale_age < 1:
        raise ValueError("Maximum stale age must be at least one second")
    if cache_enabled and stale_on_error and max_stale_age < cache_ttl:
        raise ValueError("Maximum stale age must be at least as large as the result cache TTL")
    return cache_enabled, cache_ttl, max_stale_age, stale_on_error


def _agent_arguments(params: Mapping[str, Any], _host_config: Any) -> Iterable[SpecialAgentCommand]:
    api_token = params["api_token"]
    if not isinstance(api_token, Secret):
        raise TypeError("The Hetzner API token must be provided as a Checkmk Secret")

    arguments: list[str | Secret] = [
        "--api-token",
        api_token,
        "--api-url",
        str(params.get("api_url") or DEFAULT_API_URL),
        "--timeout",
        str(params.get("timeout") or 10),
    ]
    arguments.append("--fetch-metrics" if _bool_value(params.get("fetch_metrics"), True) else "--no-fetch-metrics")
    cache_enabled, cache_ttl, cache_max_stale_age, cache_stale_on_error = _cache_arguments(params)
    arguments.append("--cache-enabled" if cache_enabled else "--no-cache-enabled")
    arguments.extend(["--cache-ttl", str(cache_ttl)])
    arguments.extend(["--cache-max-stale-age", str(cache_max_stale_age)])
    arguments.append("--cache-stale-on-error" if cache_stale_on_error else "--no-cache-stale-on-error")
    yield SpecialAgentCommand(command_arguments=arguments)


special_agent_hetzner_loadbalancer = SpecialAgentConfig(
    name="hetzner_loadbalancer",
    parameter_parser=noop_parser,
    commands_function=_agent_arguments,
)
