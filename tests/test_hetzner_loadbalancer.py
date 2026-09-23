from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch

import pytest
from cmk.agent_based.v2 import Metric, State
from cmk.server_side_calls.v1 import Secret

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "cmk_addons/plugins/hetzner_loadbalancer"
FIXTURES = Path(__file__).parent / "fixtures"


def _load_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


AGENT = _load_module("hetzner_loadbalancer_agent_test", PLUGIN_ROOT / "libexec/agent_hetzner_loadbalancer")
CHECK = _load_module("hetzner_loadbalancer_check_test", PLUGIN_ROOT / "agent_based/hetzner_loadbalancer.py")
GRAPHING = _load_module("hetzner_loadbalancer_graphing_test", PLUGIN_ROOT / "graphing/hetzner_loadbalancer.py")
RULESET = _load_module("hetzner_loadbalancer_ruleset_test", PLUGIN_ROOT / "rulesets/hetzner_loadbalancer.py")
SERVER_SIDE = _load_module(
    "hetzner_loadbalancer_server_side_test", PLUGIN_ROOT / "server_side_calls/hetzner_loadbalancer.py"
)


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def section_from_raw(raw_load_balancers: list[dict], *, metrics_enabled: bool = True):
    payload = {
        "load_balancers": [AGENT.normalize_load_balancer(value) for value in raw_load_balancers],
        "errors": [],
        "metrics_enabled": metrics_enabled,
    }
    return CHECK.parse_hetzner_loadbalancer([[json.dumps(payload)]])


def check_result(function, *args):
    return list(function(*args))[0]


def emitted_metrics(results) -> dict[str, float]:
    return {result.name: result.value for result in results if isinstance(result, Metric)}


def test_single_tcp_load_balancer_and_multiple_targets() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    section = section_from_raw(raw)
    assert list(section["load_balancers"]) == ["101001"]
    assert len(section["targets"]) == 2
    assert [service.item for service in CHECK.discover_general(section)] == ["101001"]


def test_multiple_load_balancers_and_services_have_unique_items() -> None:
    section = section_from_raw(fixture("multiple_api.json")["load_balancers"])
    assert set(section["load_balancers"]) == {"101001", "101002"}
    assert len(section["targets"]) == 5
    items = [service.item for service in CHECK.discover_targets(section)]
    assert len(items) == len(set(items))
    assert any("tcp/25/26" in item for item in items)
    assert any("tcp/465/465" in item for item in items)


def test_general_service_omits_missing_status_and_includes_algorithm() -> None:
    raw = fixture("multiple_api.json")["load_balancers"][:1]
    section = section_from_raw(raw)
    assert "status" not in section["load_balancers"]["101001"]

    result = check_result(CHECK.check_general, "101001", section)
    assert result.state == State.OK
    assert "Status: unknown" not in result.summary
    assert "Status:" not in result.summary
    assert "Name: mail-test" in result.summary
    assert "Type: lb11" in result.summary
    assert "Location: fsn-test" in result.summary
    assert "Algorithm: least_connections" in result.summary
    assert "Services: 2" in result.summary
    assert "Targets: 2" in result.summary


@pytest.mark.parametrize(
    "statuses, expected_healthy",
    [
        (("healthy", "healthy"), 2),
        (("healthy", "unhealthy"), 1),
        (("unhealthy", "unhealthy"), 0),
    ],
)
def test_general_service_emits_healthy_target_count(
    statuses: tuple[str, str], expected_healthy: int
) -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    for target, status in zip(raw[0]["targets"], statuses, strict=True):
        target["health_status"][0]["status"] = status
    section = section_from_raw(raw)
    metrics = emitted_metrics(CHECK.check_general("101001", section))
    assert metrics == {"hetzner_lb_healthy_targets": expected_healthy}


def test_api_provided_general_status_is_preserved() -> None:
    section = section_from_raw(fixture("single_tcp_api.json")["load_balancers"])
    result = check_result(CHECK.check_general, "101001", section)
    assert result.state == State.OK
    assert "Status: running" in result.summary


def test_server_target_uses_concise_stable_id_and_shows_ip() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    raw[0]["targets"] = [
        {
            "type": "server",
            "server": {"id": 159324590, "ip": "168.119.187.85"},
            "health_status": [{"listen_port": 25, "status": "healthy"}],
        }
    ]
    section = section_from_raw(raw)
    item = next(iter(section["targets"]))
    assert item == "101001/tcp/25/26/server:159324590"
    assert "server:server:" not in item

    result = check_result(CHECK.check_target, item, section)
    assert result.summary == "Health: healthy, Target: 168.119.187.85, frontend TCP/25 -> backend TCP/26"


def test_server_target_without_ip_omits_target_address_from_summary() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    raw[0]["targets"] = [
        {
            "type": "server",
            "server": {"id": 159324590},
            "health_status": [{"listen_port": 25, "status": "healthy"}],
        }
    ]
    section = section_from_raw(raw)
    item = next(iter(section["targets"]))
    result = check_result(CHECK.check_target, item, section)
    assert result.summary == "Health: healthy, frontend TCP/25 -> backend TCP/26"
    assert "Target: unknown" not in result.summary


def test_target_is_discovered_only_for_service_with_api_health_status() -> None:
    raw = fixture("multiple_api.json")["load_balancers"][:1]
    raw[0]["targets"] = [
        {
            "type": "server",
            "server": {"id": 201001},
            "health_status": [{"listen_port": 25, "status": "healthy"}],
        }
    ]
    section = section_from_raw(raw)
    assert len(section["targets"]) == 1
    item = next(iter(section["targets"]))
    assert "tcp/25/26" in item
    assert "tcp/465/465" not in item


def test_target_without_health_status_does_not_create_health_service() -> None:
    raw = fixture("multiple_api.json")["load_balancers"][:1]
    raw[0]["targets"] = [{"type": "server", "server": {"id": 201001}, "health_status": []}]
    section = section_from_raw(raw)
    assert section["targets"] == {}
    assert list(CHECK.discover_targets(section)) == []


def test_health_status_for_unknown_port_is_preserved_without_invented_service() -> None:
    raw = fixture("multiple_api.json")["load_balancers"][:1]
    raw[0]["targets"] = [
        {
            "type": "server",
            "server": {"id": 201001},
            "health_status": [{"listen_port": 9999, "status": "unhealthy"}],
        }
    ]
    section = section_from_raw(raw)
    assert len(section["targets"]) == 1
    item, entry = next(iter(section["targets"].items()))
    assert entry["service"] == {}
    result = check_result(CHECK.check_target, item, section)
    assert result.state == State.CRIT
    assert "API listen port 9999, no matching configured service" in result.summary
    assert "frontend" not in result.summary
    assert "backend" not in result.summary


def test_targets_with_different_health_subsets_do_not_gain_cross_product_services() -> None:
    raw = fixture("multiple_api.json")["load_balancers"][:1]
    raw[0]["targets"] = [
        {
            "type": "server",
            "server": {"id": 201001},
            "health_status": [{"listen_port": 25, "status": "healthy"}],
        },
        {
            "type": "server",
            "server": {"id": 201002},
            "health_status": [{"listen_port": 465, "status": "unhealthy"}],
        },
    ]
    section = section_from_raw(raw)
    assert len(section["targets"]) == 2
    discovered = {
        (entry["target"]["key"], entry["service"]["listen_port"])
        for entry in section["targets"].values()
    }
    assert discovered == {
        ("server:201001", 25),
        ("server:201002", 465),
    }


def test_healthy_and_unhealthy_targets_map_to_ok_and_crit() -> None:
    section = section_from_raw(fixture("single_tcp_api.json")["load_balancers"])
    states = {
        entry["health"]["status"]: check_result(CHECK.check_target, item, section).state
        for item, entry in section["targets"].items()
    }
    assert states == {"healthy": State.OK, "unhealthy": State.CRIT}


def test_explicit_target_health_emits_binary_metrics_without_changing_states() -> None:
    section = section_from_raw(fixture("single_tcp_api.json")["load_balancers"])
    observed = {}
    for item, entry in section["targets"].items():
        results = list(CHECK.check_target(item, section))
        service_result = next(result for result in results if not isinstance(result, Metric))
        observed[entry["health"]["status"]] = (
            service_result.state,
            emitted_metrics(results),
        )
    assert observed == {
        "healthy": (State.OK, {"hetzner_lb_target_health": 1}),
        "unhealthy": (State.CRIT, {"hetzner_lb_target_health": 0}),
    }


def test_unknown_target_health_emits_no_binary_or_aggregate_metric() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    raw[0]["targets"][0]["health_status"][0]["status"] = "future-state"
    section = section_from_raw(raw)
    unknown_item = next(
        item
        for item, entry in section["targets"].items()
        if entry["health"]["status"] == "future-state"
    )
    assert emitted_metrics(CHECK.check_target(unknown_item, section)) == {}
    assert emitted_metrics(CHECK.check_general("101001", section)) == {}


def test_target_without_health_emits_no_health_metrics() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    raw[0]["targets"] = [
        {"type": "server", "server": {"id": 159324590}, "health_status": []}
    ]
    section = section_from_raw(raw)
    assert section["targets"] == {}
    assert emitted_metrics(CHECK.check_general("101001", section)) == {}


def test_same_target_on_two_services_has_independent_metric_histories() -> None:
    raw = fixture("multiple_api.json")["load_balancers"][:1]
    raw[0]["targets"] = [
        {
            "type": "server",
            "server": {"id": 201001},
            "health_status": [
                {"listen_port": 25, "status": "healthy"},
                {"listen_port": 465, "status": "unhealthy"},
            ],
        }
    ]
    section = section_from_raw(raw)
    identities = set()
    values = set()
    for item in section["targets"]:
        metrics = emitted_metrics(CHECK.check_target(item, section))
        identities.add((item, next(iter(metrics))))
        values.update(metrics.values())
    assert len(identities) == 2
    assert any("tcp/25/26" in item for item, _metric_name in identities)
    assert any("tcp/465/465" in item for item, _metric_name in identities)
    assert values == {0, 1}


def test_healthy_target_aggregate_is_independent_per_load_balancer() -> None:
    raw_a = fixture("single_tcp_api.json")["load_balancers"][0]
    raw_b = json.loads(json.dumps(raw_a))
    raw_b["id"] = 101002
    raw_b["name"] = "second-lb"
    for target in raw_a["targets"]:
        target["health_status"][0]["status"] = "healthy"
    raw_b["targets"][0]["health_status"][0]["status"] = "healthy"
    raw_b["targets"][1]["health_status"][0]["status"] = "unhealthy"
    section = section_from_raw([raw_a, raw_b])
    assert emitted_metrics(CHECK.check_general("101001", section)) == {
        "hetzner_lb_healthy_targets": 2
    }
    assert emitted_metrics(CHECK.check_general("101002", section)) == {
        "hetzner_lb_healthy_targets": 1
    }


def test_mixed_health_preserves_frontend_and_backend_semantics() -> None:
    section = section_from_raw(fixture("single_tcp_api.json")["load_balancers"])
    results = [check_result(CHECK.check_target, item, section) for item in section["targets"]]
    assert {result.state for result in results} == {State.OK, State.CRIT}
    assert all("frontend TCP/25 -> backend TCP/26" in result.summary for result in results)


def test_unknown_health_status_is_visible_and_unknown() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    raw[0]["targets"][0]["health_status"][0]["status"] = "future-state"
    section = section_from_raw(raw)
    entry = next((item, value) for item, value in section["targets"].items() if value["health"]["status"] == "future-state")
    result = check_result(CHECK.check_target, entry[0], section)
    assert result.state == State.UNKNOWN
    assert "future-state" in result.summary


def test_optional_target_and_load_balancer_fields_can_be_missing() -> None:
    normalized = AGENT.normalize_load_balancer(
        {"name": "incomplete-test", "services": [{"protocol": "tcp"}], "targets": [{"type": "ip", "ip": {}}]}
    )
    section = CHECK.parse_hetzner_loadbalancer(
        [[json.dumps({"load_balancers": [normalized], "errors": [], "metrics_enabled": False})]]
    )
    assert normalized["id"].startswith("unknown-")
    assert section["targets"] == {}


def test_http_https_service_configuration_is_represented() -> None:
    section = section_from_raw(fixture("multiple_api.json")["load_balancers"])
    item = next(item for item, entry in section["targets"].items() if entry["lb_id"] == "101002")
    result = check_result(CHECK.check_target, item, section)
    assert result.state == State.OK
    assert "frontend HTTPS/443 -> backend HTTPS/8443" in result.summary
    assert "label selector role=test-web" in result.details


def test_null_requests_per_second_is_omitted_without_zero() -> None:
    metrics = AGENT.latest_metric_values(fixture("metrics_null_requests.json"))
    assert "requests_per_second" not in metrics
    assert metrics["open_connections"]["value"] == 3.0


def test_numeric_requests_per_second_and_latest_samples_are_used() -> None:
    metrics = AGENT.latest_metric_values(fixture("metrics_http.json"))
    assert metrics["requests_per_second"] == {"timestamp": 1790016240.0, "value": 8.25}
    assert len(metrics) == 5


def test_newest_sample_exactly_120_seconds_old_is_fresh() -> None:
    payload = fixture("metrics_http.json")
    payload["metrics"]["time_series"]["open_connections"]["values"] = [[1790016120, "7"]]
    metrics, unavailable = AGENT.select_metric_values(payload)
    assert metrics["open_connections"]["value"] == 7.0
    assert "open_connections" not in unavailable


def test_newest_sample_older_than_120_seconds_is_stale() -> None:
    payload = fixture("metrics_http.json")
    payload["metrics"]["time_series"]["open_connections"]["values"] = [[1790016119, "7"]]
    metrics, unavailable = AGENT.select_metric_values(payload)
    assert "open_connections" not in metrics
    assert unavailable["open_connections"].startswith("stale")


def test_early_valid_sample_followed_by_null_samples_is_stale() -> None:
    payload = fixture("metrics_http.json")
    payload["metrics"]["time_series"]["open_connections"]["values"] = [
        [1790016060, "7"],
        [1790016180, None],
        [1790016240, None],
    ]
    metrics, unavailable = AGENT.select_metric_values(payload)
    assert "open_connections" not in metrics
    assert unavailable["open_connections"].startswith("stale")


def test_fresh_metric_is_emitted_while_stale_metric_is_reported_in_details() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    normalized = AGENT.normalize_load_balancer(raw[0])
    normalized["metrics"] = {
        "open_connections": {"timestamp": 1790016240.0, "value": 4.0},
    }
    normalized["metrics_unavailable"] = {
        "bandwidth.in": "stale (newest valid sample is 180s old; maximum 120s)",
    }
    section = CHECK.parse_hetzner_loadbalancer(
        [[json.dumps({"load_balancers": [normalized], "errors": [], "metrics_enabled": True})]]
    )
    results = list(CHECK.check_performance("101001", section))
    result = results[0]
    metric_names = {entry.name for entry in results if isinstance(entry, Metric)}
    assert result.state == State.OK
    assert metric_names == {"hetzner_lb_open_connections"}
    assert "bandwidth.in: stale" in result.details


def test_all_stale_metrics_make_performance_service_unknown() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    normalized = AGENT.normalize_load_balancer(raw[0])
    normalized["metrics"] = {}
    normalized["metrics_unavailable"] = {
        name: "stale (newest valid sample is 180s old; maximum 120s)"
        for name in CHECK.METRIC_NAMES
    }
    section = CHECK.parse_hetzner_loadbalancer(
        [[json.dumps({"load_balancers": [normalized], "errors": [], "metrics_enabled": True})]]
    )
    results = list(CHECK.check_performance("101001", section))
    assert len(results) == 1
    assert results[0].state == State.UNKNOWN
    assert results[0].summary == "No current performance metrics available"
    assert "requests_per_second: stale" in results[0].details


def test_missing_metric_is_omitted() -> None:
    payload = fixture("metrics_http.json")
    del payload["metrics"]["time_series"]["bandwidth.out"]
    metrics = AGENT.latest_metric_values(payload)
    assert "bandwidth.out" not in metrics


def test_performance_check_emits_only_available_metrics() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    normalized = AGENT.normalize_load_balancer(raw[0])
    normalized["metrics"] = AGENT.latest_metric_values(fixture("metrics_null_requests.json"))
    section = CHECK.parse_hetzner_loadbalancer(
        [[json.dumps({"load_balancers": [normalized], "errors": [], "metrics_enabled": True})]]
    )
    results = list(CHECK.check_performance("101001", section))
    metric_names = {result.name for result in results if isinstance(result, Metric)}
    assert "hetzner_lb_requests_per_second" not in metric_names
    assert metric_names == {
        "hetzner_lb_open_connections",
        "hetzner_lb_connections_per_second",
        "hetzner_lb_bandwidth_in",
        "hetzner_lb_bandwidth_out",
    }


def test_individual_metrics_api_error_does_not_remove_health_data() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    args = argparse.Namespace(api_url="https://api.test/v1", timeout=10, fetch_metrics=True)
    with (
        patch.object(AGENT, "fetch_load_balancers", return_value=raw),
        patch.object(AGENT, "fetch_metrics", side_effect=AGENT.AgentError("http_error", "HTTP 503")),
    ):
        payload = AGENT.collect(args, "test-token")
    assert payload["load_balancers"][0]["metrics_error"]["code"] == "http_error"
    section = CHECK.parse_hetzner_loadbalancer([[json.dumps(payload)]])
    assert len(section["targets"]) == 2
    assert check_result(CHECK.check_performance, "101001", section).state == State.UNKNOWN


def test_metrics_failure_for_one_load_balancer_is_isolated_from_another() -> None:
    raw = fixture("multiple_api.json")["load_balancers"]
    successful_metrics = AGENT.select_metric_values(fixture("metrics_http.json"))
    args = argparse.Namespace(api_url="https://api.test/v1", timeout=10, fetch_metrics=True)

    def fetch_metrics(_api_url, _api_token, _timeout, lb_id):
        if lb_id == "101001":
            raise AGENT.AgentError("http_error", "HTTP 503")
        return successful_metrics

    with (
        patch.object(AGENT, "fetch_load_balancers", return_value=raw),
        patch.object(AGENT, "fetch_metrics", side_effect=fetch_metrics),
    ):
        payload = AGENT.collect(args, "test-token")

    section = CHECK.parse_hetzner_loadbalancer([[json.dumps(payload)]])
    assert {service.item for service in CHECK.discover_general(section)} == {"101001", "101002"}
    assert len(list(CHECK.discover_targets(section))) == 5
    assert check_result(CHECK.check_performance, "101001", section).state == State.UNKNOWN
    lb_b_results = list(CHECK.check_performance("101002", section))
    assert lb_b_results[0].state == State.OK
    assert any(isinstance(result, Metric) for result in lb_b_results)


def test_load_balancer_api_error_is_reported_without_token() -> None:
    output = io.StringIO()
    with (
        patch.object(AGENT, "fetch_load_balancers", side_effect=AGENT.AgentError("auth_error", "bad secret-token")),
        contextlib.redirect_stdout(output),
    ):
        assert AGENT.main(["--api-token", "secret-token", "--no-cache-enabled"]) == 0
    rendered = output.getvalue()
    assert rendered.startswith("<<<hetzner_loadbalancer:sep(0)>>>")
    assert "secret-token" not in rendered
    assert "auth_error" in rendered


def test_empty_load_balancer_list_keeps_api_service() -> None:
    section = CHECK.parse_hetzner_loadbalancer(
        [[json.dumps({"load_balancers": [], "errors": [], "metrics_enabled": True})]]
    )
    assert len(list(CHECK.discover_api(section))) == 1
    result = check_result(CHECK.check_api, section)
    assert result.state == State.OK
    assert "no Load Balancers" in result.summary


def test_api_error_is_unknown_on_dedicated_api_service() -> None:
    section = CHECK.parse_hetzner_loadbalancer(
        [[json.dumps({"load_balancers": [], "errors": [{"code": "timeout", "message": "timed out"}], "metrics_enabled": True})]]
    )
    assert check_result(CHECK.check_api, section).state == State.UNKNOWN


def test_malformed_agent_json_is_handled() -> None:
    section = CHECK.parse_hetzner_loadbalancer([["not-json"]])
    assert section["errors"][0]["code"] == "json_error"
    assert check_result(CHECK.check_api, section).state == State.UNKNOWN


def test_metrics_query_is_bounded_and_keeps_only_latest_sample() -> None:
    payload = fixture("metrics_http.json")
    with patch.object(AGENT, "request_json", return_value=payload) as request:
        metrics, unavailable = AGENT.fetch_metrics(
            "https://api.test/v1",
            "token",
            10,
            "101002",
            now=datetime(2026, 9, 21, 18, 44, tzinfo=timezone.utc),
        )
    url = request.call_args.args[0]
    assert "step=60" in url
    assert "load_balancers%2F" not in url
    assert metrics["open_connections"]["value"] == 12.0
    assert unavailable == {}
    assert all(set(sample) == {"timestamp", "value"} for sample in metrics.values())


def test_metrics_can_be_disabled_and_server_side_call_uses_secret_argument() -> None:
    section = section_from_raw(fixture("single_tcp_api.json")["load_balancers"], metrics_enabled=False)
    assert list(CHECK.discover_performance(section)) == []
    secret = Secret(42)
    commands = list(SERVER_SIDE._agent_arguments({"api_token": secret, "fetch_metrics": False, "timeout": 7}, None))
    arguments = commands[0].command_arguments
    assert "--no-fetch-metrics" in arguments
    assert secret in arguments
    assert "--cache-enabled" in arguments
    assert arguments[arguments.index("--cache-ttl") + 1] == "60"
    assert arguments[arguments.index("--cache-max-stale-age") + 1] == "900"
    assert "--cache-stale-on-error" in arguments


def test_ruleset_cache_defaults_are_enabled_with_60_second_ttl_and_stale_fallback() -> None:
    migrated = RULESET._migrate_integration_params({})
    assert migrated["fetch_metrics"] is True
    assert migrated["cache_enabled"] == (
        "enabled",
        {"cache_ttl": 60, "cache_max_stale_age": 900, "cache_stale_on_error": True},
    )
    assert RULESET.DEFAULT_CACHE_TTL_SECONDS == 60
    assert RULESET.DEFAULT_CACHE_MAX_STALE_AGE_SECONDS == 900
    assert RULESET._cache_ttl_value(0) == 1
    args = AGENT.parse_arguments(["--api-token", "fixture-token"])
    assert args.cache_enabled is True
    assert args.cache_ttl == 60
    assert args.cache_max_stale_age == 900
    assert args.cache_stale_on_error is True


def test_fetch_metrics_is_a_required_top_level_boolean_without_changing_its_shape() -> None:
    parameter_form = RULESET._special_agent_parameter_form()
    fetch_metrics = parameter_form.elements["fetch_metrics"]
    assert fetch_metrics.required is True
    assert isinstance(fetch_metrics.parameter_form, RULESET.BooleanChoice)
    assert RULESET._migrate_integration_params({"fetch_metrics": False})["fetch_metrics"] is False
    assert RULESET._migrate_integration_params({"fetch_metrics": True})["fetch_metrics"] is True


def test_cache_validation_requires_positive_ttl_and_consistent_stale_age() -> None:
    with pytest.raises(RULESET.validators.ValidationError, match="Maximum stale age"):
        RULESET._validate_cache_settings(
            {"cache_ttl": 60, "cache_max_stale_age": 59, "cache_stale_on_error": True}
        )
    RULESET._validate_cache_settings(
        {"cache_ttl": 60, "cache_max_stale_age": 59, "cache_stale_on_error": False}
    )
    with pytest.raises(ValueError, match="TTL must be at least one second"):
        SERVER_SIDE._cache_arguments(
            {"cache_enabled": ("enabled", {"cache_ttl": 0, "cache_max_stale_age": 900})}
        )


@pytest.mark.parametrize(
    "configured, expected",
    [
        ({}, (True, 60, 900, True)),
        (
            {
                "cache_enabled": (
                    "enabled",
                    {"cache_ttl": 45, "cache_max_stale_age": 300, "cache_stale_on_error": True},
                )
            },
            (True, 45, 300, True),
        ),
        ({"cache_enabled": ("disabled", None)}, (False, 60, 900, True)),
        (
            {
                "cache_enabled": (
                    "enabled",
                    {"cache_ttl": 60, "cache_max_stale_age": 30, "cache_stale_on_error": False},
                )
            },
            (True, 60, 30, False),
        ),
    ],
)
def test_server_side_cache_configuration(
    configured: dict, expected: tuple[bool, int, int, bool]
) -> None:
    assert SERVER_SIDE._cache_arguments(configured) == expected


def test_server_side_call_emits_custom_and_disabled_cache_arguments() -> None:
    secret = Secret(42)
    custom = list(
        SERVER_SIDE._agent_arguments(
            {
                "api_token": secret,
                "fetch_metrics": True,
                "cache_enabled": (
                    "enabled",
                    {
                        "cache_ttl": 75,
                        "cache_max_stale_age": 600,
                        "cache_stale_on_error": False,
                    },
                ),
            },
            None,
        )
    )[0].command_arguments
    assert "--fetch-metrics" in custom
    assert "--cache-enabled" in custom
    assert custom[custom.index("--cache-ttl") + 1] == "75"
    assert custom[custom.index("--cache-max-stale-age") + 1] == "600"
    assert "--no-cache-stale-on-error" in custom
    assert secret in custom

    disabled = list(
        SERVER_SIDE._agent_arguments(
            {"api_token": secret, "cache_enabled": ("disabled", None)},
            None,
        )
    )[0].command_arguments
    assert "--no-cache-enabled" in disabled


def _cache_test_args(
    tmp_path: Path,
    *,
    stale_on_error: bool = True,
    max_stale_age: int = 900,
) -> argparse.Namespace:
    return argparse.Namespace(
        api_url="https://api.test/v1",
        timeout=10,
        fetch_metrics=False,
        cache_enabled=True,
        cache_ttl=60,
        cache_max_stale_age=max_stale_age,
        cache_stale_on_error=stale_on_error,
        cache_dir=str(tmp_path),
    )


def test_enabled_agent_cache_rejects_zero_ttl(tmp_path: Path) -> None:
    args = _cache_test_args(tmp_path)
    args.cache_ttl = 0
    with pytest.raises(AGENT.AgentError, match="TTL must be at least one second"):
        AGENT.collect_with_cache(args, "fixture-token")


def test_stale_cache_fallback_is_visible_and_does_not_store_clear_token(tmp_path: Path) -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    args = _cache_test_args(tmp_path)
    token = "fixture-secret-token"
    with (
        patch.object(AGENT.time, "time", return_value=1000.0),
        patch.object(AGENT, "fetch_load_balancers", return_value=raw),
    ):
        fresh_payload, fresh_info = AGENT.collect_with_cache(args, token)
    assert fresh_info["status"] == "refresh"

    with (
        patch.object(AGENT.time, "time", return_value=1184.0),
        patch.object(
            AGENT,
            "fetch_load_balancers",
            side_effect=AGENT.AgentError("network_error", f"failed for {token}"),
        ),
    ):
        stale_payload, stale_info = AGENT.collect_with_cache(args, token)

    assert stale_payload == fresh_payload
    assert stale_info["status"] == "stale_on_error"
    assert stale_info["stale"] is True
    assert stale_info["age_seconds"] == 184.0
    assert stale_info["max_stale_age_seconds"] == 900
    assert token not in stale_info["error"]
    assert all(token not in path.read_text(encoding="utf-8") for path in tmp_path.glob("*.json"))

    stale_payload["cache"] = stale_info
    section = CHECK.parse_hetzner_loadbalancer([[json.dumps(stale_payload)]])
    api_results = list(CHECK.check_api(section))
    assert len(api_results) == 1
    assert api_results[0].state == State.WARN
    assert api_results[0].summary == "Using stale cached data, cache age: 184 s, API refresh failed"
    assert "Cache TTL: 60 seconds" in api_results[0].details
    assert "Maximum stale age: 900 seconds" in api_results[0].details
    assert token not in api_results[0].details

    general_results = list(CHECK.check_general("101001", section))
    general_service_results = [result for result in general_results if not isinstance(result, Metric)]
    assert len(general_service_results) == 1
    assert general_service_results[0].state == State.OK
    assert "Data source: stale cache" in general_service_results[0].details
    assert "Cache age: 184 seconds" in general_service_results[0].details
    assert emitted_metrics(general_results) == {"hetzner_lb_healthy_targets": 1}

    target_states = {}
    for target_item, entry in section["targets"].items():
        target_results = list(CHECK.check_target(target_item, section))
        target_service_results = [result for result in target_results if not isinstance(result, Metric)]
        assert len(target_service_results) == 1
        target_states[entry["health"]["status"]] = target_service_results[0].state
        assert "Data source: stale cache" in target_service_results[0].details
        assert emitted_metrics(target_results) == {
            "hetzner_lb_target_health": 1 if entry["health"]["status"] == "healthy" else 0
        }
    assert target_states == {"healthy": State.OK, "unhealthy": State.CRIT}


def test_result_cache_reuses_collection_within_configured_ttl(tmp_path: Path) -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    args = _cache_test_args(tmp_path)
    with patch.object(AGENT, "fetch_load_balancers", return_value=raw) as fetch:
        with patch.object(AGENT.time, "time", return_value=1000.0):
            first_payload, first_info = AGENT.collect_with_cache(args, "fixture-token")
        with patch.object(AGENT.time, "time", return_value=1059.0):
            second_payload, second_info = AGENT.collect_with_cache(args, "fixture-token")

    assert fetch.call_count == 1
    assert first_payload == second_payload
    assert first_info["status"] == "refresh"
    assert second_info["status"] == "hit"
    assert second_info["ttl_seconds"] == 60
    assert second_info["max_stale_age_seconds"] == 900


@pytest.mark.parametrize("cache_age", [184.0, 900.0])
def test_stale_cache_fallback_is_allowed_through_maximum_age(
    tmp_path: Path, cache_age: float
) -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    args = _cache_test_args(tmp_path)
    with (
        patch.object(AGENT.time, "time", return_value=1000.0),
        patch.object(AGENT, "fetch_load_balancers", return_value=raw),
    ):
        fresh_payload, _fresh_info = AGENT.collect_with_cache(args, "fixture-token")
    with (
        patch.object(AGENT.time, "time", return_value=1000.0 + cache_age),
        patch.object(
            AGENT,
            "fetch_load_balancers",
            side_effect=AGENT.AgentError("network_error", "refresh failed"),
        ),
    ):
        stale_payload, stale_info = AGENT.collect_with_cache(args, "fixture-token")
    assert stale_payload == fresh_payload
    assert stale_info["status"] == "stale_on_error"
    assert stale_info["age_seconds"] == cache_age


def test_stale_cache_above_maximum_age_is_rejected(tmp_path: Path) -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    args = _cache_test_args(tmp_path)
    with (
        patch.object(AGENT.time, "time", return_value=1000.0),
        patch.object(AGENT, "fetch_load_balancers", return_value=raw),
    ):
        AGENT.collect_with_cache(args, "fixture-token")
    with (
        patch.object(AGENT.time, "time", return_value=1900.001),
        patch.object(
            AGENT,
            "fetch_load_balancers",
            side_effect=AGENT.AgentError("network_error", "refresh failed"),
        ),
        pytest.raises(AGENT.AgentError, match="refresh failed"),
    ):
        AGENT.collect_with_cache(args, "fixture-token")


def test_stale_cache_fallback_can_be_disabled(tmp_path: Path) -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    args = _cache_test_args(tmp_path, stale_on_error=False)
    with (
        patch.object(AGENT.time, "time", return_value=1000.0),
        patch.object(AGENT, "fetch_load_balancers", return_value=raw),
    ):
        AGENT.collect_with_cache(args, "fixture-token")

    with (
        patch.object(AGENT.time, "time", return_value=1184.0),
        patch.object(
            AGENT,
            "fetch_load_balancers",
            side_effect=AGENT.AgentError("network_error", "refresh failed"),
        ),
        pytest.raises(AGENT.AgentError, match="refresh failed"),
    ):
        AGENT.collect_with_cache(args, "fixture-token")


def test_stale_cache_does_not_warn_performance_service() -> None:
    raw = fixture("single_tcp_api.json")["load_balancers"]
    normalized = AGENT.normalize_load_balancer(raw[0])
    normalized["metrics"] = AGENT.latest_metric_values(fixture("metrics_http.json"))
    payload = {
        "load_balancers": [normalized],
        "errors": [],
        "metrics_enabled": True,
        "cache": {
            "enabled": True,
            "status": "stale_on_error",
            "stale": True,
            "age_seconds": 184,
            "ttl_seconds": 60,
            "max_stale_age_seconds": 900,
            "error": "refresh failed",
        },
    }
    section = CHECK.parse_hetzner_loadbalancer([[json.dumps(payload)]])
    results = list(CHECK.check_performance("101001", section))
    service_results = [result for result in results if not isinstance(result, Metric)]
    assert len(service_results) == 1
    assert service_results[0].state == State.OK
    assert "Data source: stale cache" in service_results[0].details
    assert any(isinstance(result, Metric) for result in results)


def test_server_side_call_rejects_plain_text_api_token() -> None:
    with pytest.raises(TypeError, match="Checkmk Secret"):
        list(SERVER_SIDE._agent_arguments({"api_token": "plain-text-token"}, None))


def test_graphing_and_ruleset_modules_use_public_checkmk_v1_apis() -> None:
    assert GRAPHING.metric_hetzner_lb_bandwidth_in.name == "hetzner_lb_bandwidth_in"
    assert GRAPHING.metric_hetzner_lb_healthy_targets.name == "hetzner_lb_healthy_targets"
    assert GRAPHING.metric_hetzner_lb_target_health.name == "hetzner_lb_target_health"
    assert GRAPHING.graph_hetzner_lb_healthy_targets.simple_lines == (
        "hetzner_lb_healthy_targets",
    )
    assert GRAPHING.graph_hetzner_lb_target_health.minimal_range.lower == 0
    assert GRAPHING.graph_hetzner_lb_target_health.minimal_range.upper == 1
    assert GRAPHING.perfometer_hetzner_lb_open_connections.segments == (
        "hetzner_lb_open_connections",
    )
    assert RULESET.rule_spec_hetzner_loadbalancer.name == "hetzner_loadbalancer"


@pytest.mark.parametrize("target, expected_key", [
    ({"type": "server", "server": {"id": 12345}}, "server:12345"),
    ({"type": "ip", "ip": {"ip": "198.51.100.20"}}, "ip:198.51.100.20"),
    ({"type": "label_selector", "label_selector": {"selector": "role=fixture"}}, "label_selector:role=fixture"),
    ({"type": "future_target", "future_target": {"id": "fictional"}}, "future_target:id:fictional"),
])
def test_supported_and_unknown_target_shapes_have_stable_meaningful_keys(
    target: dict, expected_key: str
) -> None:
    normalized = AGENT._normalized_target(target)
    assert normalized is not None
    assert normalized["key"] == expected_key
    assert normalized["display"]
