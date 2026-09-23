#!/usr/bin/env python3
"""Agent-based checks for Hetzner Cloud Load Balancers."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, TypedDict

from cmk.agent_based.v2 import AgentSection, CheckPlugin, Metric, Result, Service, State


class ErrorInfo(TypedDict):
    code: str
    message: str


class Section(TypedDict):
    load_balancers: dict[str, dict[str, Any]]
    targets: dict[str, dict[str, Any]]
    errors: list[ErrorInfo]
    metrics_enabled: bool
    cache: dict[str, Any] | None


def _error(code: str, message: str) -> Section:
    return {
        "load_balancers": {},
        "targets": {},
        "errors": [{"code": code, "message": message}],
        "metrics_enabled": False,
        "cache": None,
    }


def _normalize_errors(value: Any) -> list[ErrorInfo]:
    if not isinstance(value, list):
        return [{"code": "payload_error", "message": "Agent payload field 'errors' is not a list"}]
    errors: list[ErrorInfo] = []
    for entry in value:
        if isinstance(entry, Mapping):
            errors.append(
                {
                    "code": str(entry.get("code") or "error"),
                    "message": str(entry.get("message") or "Agent reported an API error"),
                }
            )
        else:
            errors.append({"code": "error", "message": str(entry)})
    return errors


def _service_key(service: Mapping[str, Any]) -> str:
    return "/".join(
        (
            str(service.get("protocol") or "unknown").lower(),
            str(service.get("listen_port") if service.get("listen_port") is not None else "unknown"),
            str(service.get("destination_port") if service.get("destination_port") is not None else "unknown"),
        )
    )


def _services(load_balancer: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = load_balancer.get("services")
    if not isinstance(value, list):
        return []
    return [dict(entry) for entry in value if isinstance(entry, Mapping)]


def _target_entries(load_balancer: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    lb_id = str(load_balancer.get("id") or "unknown")
    raw_targets = load_balancer.get("targets")
    if not isinstance(raw_targets, list):
        return {}

    services = _services(load_balancer)
    targets: dict[str, dict[str, Any]] = {}
    for target in sorted(
        (dict(value) for value in raw_targets if isinstance(value, Mapping)),
        key=lambda value: str(value.get("key") or value.get("display") or ""),
    ):
        target_key = str(target.get("key") or "unknown-target")
        health_status = target.get("health_status")
        health_entries = [dict(value) for value in health_status if isinstance(value, Mapping)] if isinstance(health_status, list) else []

        services_by_port: dict[str, list[dict[str, Any]]] = {}
        for service in services:
            listen_port = service.get("listen_port")
            if listen_port is not None:
                services_by_port.setdefault(str(listen_port), []).append(service)

        for health in health_entries:
            matching_services = services_by_port.get(str(health.get("listen_port")), [])
            # health_status identifies a frontend only by listen_port. If the
            # configuration is absent or ambiguous, retain the API health but
            # do not invent an endpoint association.
            service = matching_services[0] if len(matching_services) == 1 else {}
            service_key = _service_key(service or {"listen_port": health.get("listen_port")})
            base_item = f"{lb_id}/{service_key}/{target_key}"
            item = base_item
            suffix = 2
            while item in targets:
                item = f"{base_item}#{suffix}"
                suffix += 1
            targets[item] = {
                "lb_id": lb_id,
                "lb_name": str(load_balancer.get("name") or lb_id),
                "target": target,
                "health": health,
                "service": service,
            }
    return targets


def parse_hetzner_loadbalancer(string_table: list[list[str]]) -> Section:
    if not string_table:
        return _error("no_data", "No agent data received")
    raw_payload = "".join(cell for row in string_table for cell in row).strip()
    if not raw_payload:
        return _error("no_data", "Empty agent section")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        return _error("json_error", f"Invalid JSON in agent section: {exc}")
    if not isinstance(payload, dict):
        return _error("payload_error", "Agent payload is not a JSON object")

    raw_load_balancers = payload.get("load_balancers")
    if not isinstance(raw_load_balancers, list):
        return _error("payload_error", "Agent payload field 'load_balancers' is not a list")
    errors = _normalize_errors(payload.get("errors", []))
    load_balancers: dict[str, dict[str, Any]] = {}
    targets: dict[str, dict[str, Any]] = {}
    for raw_load_balancer in raw_load_balancers:
        if not isinstance(raw_load_balancer, dict):
            errors.append({"code": "payload_error", "message": "A Load Balancer entry is not an object"})
            continue
        lb_id = str(raw_load_balancer.get("id") or "unknown")
        if lb_id in load_balancers:
            errors.append({"code": "payload_error", "message": f"Duplicate Load Balancer ID {lb_id}"})
            continue
        load_balancers[lb_id] = raw_load_balancer
        targets.update(_target_entries(raw_load_balancer))
    return {
        "load_balancers": load_balancers,
        "targets": targets,
        "errors": errors,
        "metrics_enabled": payload.get("metrics_enabled") is True,
        "cache": dict(payload["cache"]) if isinstance(payload.get("cache"), Mapping) else None,
    }


agent_section_hetzner_loadbalancer = AgentSection(
    name="hetzner_loadbalancer",
    parse_function=parse_hetzner_loadbalancer,
)


def discover_api(section: Section) -> Iterable[Service]:
    yield Service()


def _format_errors(errors: list[ErrorInfo]) -> str:
    return "; ".join(f"{entry['code']}: {entry['message']}" for entry in errors)


def _cache_number(cache: Mapping[str, Any], key: str) -> int:
    try:
        return max(0, int(float(cache.get(key, 0))))
    except (TypeError, ValueError):
        return 0


def _stale_cache_details(section: Section) -> str | None:
    cache = section["cache"]
    if not isinstance(cache, Mapping) or cache.get("stale") is not True:
        return None
    age = _cache_number(cache, "age_seconds")
    ttl = _cache_number(cache, "ttl_seconds")
    max_stale_age = _cache_number(cache, "max_stale_age_seconds")
    return (
        "Data source: stale cache\n"
        f"Cache age: {age} seconds\n"
        f"Cache TTL: {ttl} seconds\n"
        f"Maximum stale age: {max_stale_age} seconds"
    )


def _append_stale_cache_details(details: str | None, section: Section) -> str | None:
    cache_details = _stale_cache_details(section)
    if cache_details is None:
        return details
    return f"{details}\n{cache_details}" if details else cache_details


def _stale_cache_api_result(section: Section) -> Result | None:
    cache = section["cache"]
    cache_details = _stale_cache_details(section)
    if not isinstance(cache, Mapping) or cache_details is None:
        return None
    age = _cache_number(cache, "age_seconds")
    status = str(cache.get("status") or "")
    reason = "API refresh failed" if status == "stale_on_error" else "fresh collection unavailable"
    details = cache_details
    if cache.get("message"):
        details += f"\n{cache['message']}"
    if cache.get("error"):
        details += f"\nFresh collection error: {cache['error']}"
    return Result(
        state=State.WARN,
        summary=f"Using stale cached data, cache age: {age} s, {reason}",
        details=details,
    )


def check_api(section: Section) -> Iterable[Result]:
    if (cache_result := _stale_cache_api_result(section)) is not None:
        yield cache_result
        return
    if section["errors"]:
        yield Result(state=State.UNKNOWN, summary=_format_errors(section["errors"]))
    elif not section["load_balancers"]:
        yield Result(state=State.OK, summary="API reachable, no Load Balancers returned")
    else:
        count = len(section["load_balancers"])
        yield Result(state=State.OK, summary=f"API reachable, {count} Load Balancer{'s' if count != 1 else ''} returned")


check_plugin_hetzner_loadbalancer_api = CheckPlugin(
    name="hetzner_loadbalancer_api",
    sections=["hetzner_loadbalancer"],
    service_name="Hetzner Load Balancer API",
    discovery_function=discover_api,
    check_function=check_api,
)


def discover_general(section: Section) -> Iterable[Service]:
    for lb_id in sorted(section["load_balancers"], key=lambda value: (len(value), value)):
        yield Service(item=lb_id)


def _setting(value: Any) -> str:
    if value is True:
        return "enabled"
    if value is False:
        return "disabled"
    return "unknown"


def _service_description(service: Mapping[str, Any]) -> str:
    protocol = str(service.get("protocol") or "unknown").upper()
    listen_port = service.get("listen_port")
    destination_port = service.get("destination_port")
    frontend = f"{protocol}/{listen_port if listen_port is not None else '?'}"
    backend = f"{protocol}/{destination_port if destination_port is not None else '?'}"
    proxy = f", Proxy Protocol {_setting(service.get('proxyprotocol'))}"
    return f"{frontend} -> {backend}{proxy}"


HEALTHY_TARGETS_METRIC = "hetzner_lb_healthy_targets"
TARGET_HEALTH_METRIC = "hetzner_lb_target_health"


def _known_target_health_values(item: str, section: Section) -> list[int] | None:
    entries = [entry for entry in section["targets"].values() if entry.get("lb_id") == item]
    if not entries:
        return None
    values: list[int] = []
    for entry in entries:
        health = entry.get("health")
        health = health if isinstance(health, Mapping) else {}
        status = str(health.get("status") or "unknown").lower()
        if status == "healthy":
            values.append(1)
        elif status == "unhealthy":
            values.append(0)
        else:
            return None
    return values


def check_general(item: str, section: Section) -> Iterable[Result | Metric]:
    load_balancer = section["load_balancers"].get(item)
    if load_balancer is None:
        yield Result(state=State.UNKNOWN, summary="Load Balancer is missing from current agent data")
        return
    services = _services(load_balancer)
    targets = load_balancer.get("targets")
    target_count = len(targets) if isinstance(targets, list) else 0
    public_net = load_balancer.get("public_net")
    public_net = public_net if isinstance(public_net, Mapping) else {}
    summary_parts = [f"Name: {load_balancer.get('name', item)}"]
    if load_balancer.get("status") not in (None, ""):
        summary_parts.append(f"Status: {load_balancer['status']}")
    summary_parts.extend(
        (
            f"Type: {load_balancer.get('type', 'unknown')}",
            f"Location: {load_balancer.get('location', 'unknown')}",
            f"Algorithm: {load_balancer.get('algorithm', 'unknown')}",
            f"Services: {len(services)}",
            f"Targets: {target_count}",
        )
    )
    summary = ", ".join(summary_parts)
    details = [
        f"Load Balancer ID: {item}",
        f"Algorithm: {load_balancer.get('algorithm', 'unknown')}",
        f"Public network: {_setting(public_net.get('enabled'))}",
    ]
    if public_net.get("ipv4"):
        details.append(f"IPv4: {public_net['ipv4']}")
    if public_net.get("ipv6"):
        details.append(f"IPv6: {public_net['ipv6']}")
    details.extend(f"Service: {_service_description(service)}" for service in services)
    details_text = _append_stale_cache_details("\n".join(details), section)
    warnings = load_balancer.get("warnings")
    if isinstance(warnings, list) and warnings:
        yield Result(
            state=State.UNKNOWN,
            summary="; ".join(str(value) for value in warnings),
            details=details_text,
        )
    else:
        yield Result(state=State.OK, summary=summary, details=details_text)
    if (health_values := _known_target_health_values(item, section)) is not None:
        yield Metric(HEALTHY_TARGETS_METRIC, sum(health_values))


check_plugin_hetzner_loadbalancer_general = CheckPlugin(
    name="hetzner_loadbalancer_general",
    sections=["hetzner_loadbalancer"],
    service_name="Hetzner Load Balancer %s",
    discovery_function=discover_general,
    check_function=check_general,
)


def discover_targets(section: Section) -> Iterable[Service]:
    for item in sorted(section["targets"]):
        yield Service(item=item)


def _endpoint_text(entry: Mapping[str, Any]) -> str:
    service = entry.get("service")
    service = service if isinstance(service, Mapping) else {}
    health = entry.get("health")
    health = health if isinstance(health, Mapping) else {}
    if not service:
        listen_port = health.get("listen_port")
        return (
            f"API listen port {listen_port if listen_port is not None else 'unknown'}, "
            "no matching configured service"
        )
    protocol = str(service.get("protocol") or "unknown").upper()
    listen_port = service.get("listen_port", health.get("listen_port"))
    destination_port = service.get("destination_port")
    health_check = service.get("health_check")
    health_check = health_check if isinstance(health_check, Mapping) else {}
    check_protocol = str(health_check.get("protocol") or protocol).upper()
    check_port = health_check.get("port")
    effective_backend_port = destination_port if destination_port is not None else check_port
    text = (
        f"frontend {protocol}/{listen_port if listen_port is not None else '?'} -> "
        f"backend {protocol}/{effective_backend_port if effective_backend_port is not None else '?'}"
    )
    if check_port is not None and (check_port != destination_port or check_protocol != protocol):
        text += f", health check {check_protocol}/{check_port}"
    return text


def check_target(item: str, section: Section) -> Iterable[Result | Metric]:
    entry = section["targets"].get(item)
    if entry is None:
        yield Result(state=State.UNKNOWN, summary="Target is missing from current agent data")
        return
    health = entry.get("health")
    health = health if isinstance(health, Mapping) else {}
    status = str(health.get("status") or "unknown").lower()
    if status == "healthy":
        state = State.OK
    elif status == "unhealthy":
        state = State.CRIT
    else:
        state = State.UNKNOWN
    target = entry.get("target")
    target = target if isinstance(target, Mapping) else {}
    target_name = str(target.get("display") or target.get("key") or "unknown target")
    target_data = target.get("target")
    target_data = target_data if isinstance(target_data, Mapping) else {}
    target_type = str(target.get("type") or "").lower()
    target_address = target_data.get("ip") if target_type in {"server", "ip"} else None
    endpoint = _endpoint_text(entry)
    details = f"Load Balancer: {entry.get('lb_name', entry.get('lb_id', 'unknown'))} ({entry.get('lb_id', 'unknown')})\nTarget: {target_name}"
    address_text = f", Target: {target_address}" if target_address not in (None, "") else ""
    yield Result(
        state=state,
        summary=f"Health: {status}{address_text}, {endpoint}",
        details=_append_stale_cache_details(details, section),
    )
    if status == "healthy":
        yield Metric(TARGET_HEALTH_METRIC, 1)
    elif status == "unhealthy":
        yield Metric(TARGET_HEALTH_METRIC, 0)


check_plugin_hetzner_loadbalancer_target = CheckPlugin(
    name="hetzner_loadbalancer_target",
    sections=["hetzner_loadbalancer"],
    service_name="Hetzner LB Target %s",
    discovery_function=discover_targets,
    check_function=check_target,
)


def discover_performance(section: Section) -> Iterable[Service]:
    if not section["metrics_enabled"]:
        return
    for lb_id in sorted(section["load_balancers"], key=lambda value: (len(value), value)):
        yield Service(item=lb_id)


METRIC_NAMES = {
    "open_connections": "hetzner_lb_open_connections",
    "connections_per_second": "hetzner_lb_connections_per_second",
    "requests_per_second": "hetzner_lb_requests_per_second",
    "bandwidth.in": "hetzner_lb_bandwidth_in",
    "bandwidth.out": "hetzner_lb_bandwidth_out",
}


def _metric_value(metrics: Mapping[str, Any], name: str) -> tuple[float, float] | None:
    sample = metrics.get(name)
    if not isinstance(sample, Mapping):
        return None
    try:
        if sample.get("value") is None or sample.get("timestamp") is None:
            return None
        value = float(sample["value"])
        timestamp = float(sample["timestamp"])
        return (value, timestamp) if math.isfinite(value) and math.isfinite(timestamp) else None
    except (TypeError, ValueError):
        return None


def check_performance(item: str, section: Section) -> Iterable[Result | Metric]:
    load_balancer = section["load_balancers"].get(item)
    if load_balancer is None:
        yield Result(
            state=State.UNKNOWN,
            summary="Load Balancer is missing from current agent data",
            details=_append_stale_cache_details(None, section),
        )
        return
    metrics_error = load_balancer.get("metrics_error")
    if isinstance(metrics_error, Mapping):
        yield Result(
            state=State.UNKNOWN,
            summary=f"Metrics collection failed: {metrics_error.get('code', 'error')}: {metrics_error.get('message', 'unknown error')}",
            details=_append_stale_cache_details(None, section),
        )
        return
    metrics = load_balancer.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    metrics_unavailable = load_balancer.get("metrics_unavailable")
    metrics_unavailable = metrics_unavailable if isinstance(metrics_unavailable, Mapping) else {}
    available = {name: sample for name in METRIC_NAMES if (sample := _metric_value(metrics, name)) is not None}
    if not available:
        details = "\n".join(
            f"{name}: {reason}" for name, reason in metrics_unavailable.items()
        )
        yield Result(
            state=State.UNKNOWN,
            summary="No current performance metrics available",
            details=_append_stale_cache_details(details or None, section),
        )
        return

    summaries: list[str] = []
    labels = {
        "open_connections": "Open connections",
        "connections_per_second": "Connections/s",
        "requests_per_second": "Requests/s",
        "bandwidth.in": "Bandwidth in",
        "bandwidth.out": "Bandwidth out",
    }
    for api_name, (value, _timestamp) in available.items():
        unit = " B/s" if api_name.startswith("bandwidth.") else ""
        summaries.append(f"{labels[api_name]}: {value:g}{unit}")
    newest_timestamp = max(timestamp for _value, timestamp in available.values())
    collected = datetime.fromtimestamp(newest_timestamp, tz=timezone.utc).isoformat(timespec="seconds")
    details = [f"Newest fresh API sample: {collected}"]
    details.extend(f"{name}: {reason}" for name, reason in metrics_unavailable.items())
    yield Result(
        state=State.OK,
        summary=", ".join(summaries),
        details=_append_stale_cache_details("\n".join(details), section),
    )
    for api_name, (value, _timestamp) in available.items():
        yield Metric(METRIC_NAMES[api_name], value)


check_plugin_hetzner_loadbalancer_performance = CheckPlugin(
    name="hetzner_loadbalancer_performance",
    sections=["hetzner_loadbalancer"],
    service_name="Hetzner LB Performance %s",
    discovery_function=discover_performance,
    check_function=check_performance,
)
