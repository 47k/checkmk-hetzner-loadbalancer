#!/usr/bin/env python3
"""Graphing definitions for Hetzner Cloud Load Balancer metrics."""

from __future__ import annotations

from cmk.graphing.v1 import Title, graphs, metrics, perfometers

UNIT_COUNT = metrics.Unit(metrics.DecimalNotation(""))
UNIT_PER_SECOND = metrics.Unit(metrics.DecimalNotation("/s"))
UNIT_BYTES_PER_SECOND = metrics.Unit(metrics.IECNotation("B/s"))
UNIT_INTEGER = metrics.Unit(metrics.DecimalNotation(""), metrics.StrictPrecision(0))

metric_hetzner_lb_open_connections = metrics.Metric(
    name="hetzner_lb_open_connections",
    title=Title("Open connections"),
    unit=UNIT_COUNT,
    color=metrics.Color.BLUE,
)

metric_hetzner_lb_connections_per_second = metrics.Metric(
    name="hetzner_lb_connections_per_second",
    title=Title("New connections"),
    unit=UNIT_PER_SECOND,
    color=metrics.Color.GREEN,
)

metric_hetzner_lb_requests_per_second = metrics.Metric(
    name="hetzner_lb_requests_per_second",
    title=Title("Requests"),
    unit=UNIT_PER_SECOND,
    color=metrics.Color.ORANGE,
)

metric_hetzner_lb_bandwidth_in = metrics.Metric(
    name="hetzner_lb_bandwidth_in",
    title=Title("Bandwidth in"),
    unit=UNIT_BYTES_PER_SECOND,
    color=metrics.Color.CYAN,
)

metric_hetzner_lb_bandwidth_out = metrics.Metric(
    name="hetzner_lb_bandwidth_out",
    title=Title("Bandwidth out"),
    unit=UNIT_BYTES_PER_SECOND,
    color=metrics.Color.PURPLE,
)

metric_hetzner_lb_healthy_targets = metrics.Metric(
    name="hetzner_lb_healthy_targets",
    title=Title("Healthy targets"),
    unit=UNIT_INTEGER,
    color=metrics.Color.GREEN,
)

metric_hetzner_lb_target_health = metrics.Metric(
    name="hetzner_lb_target_health",
    title=Title("Target health (1 healthy, 0 unhealthy)"),
    unit=UNIT_INTEGER,
    color=metrics.Color.GREEN,
)

perfometer_hetzner_lb_open_connections = perfometers.Perfometer(
    name="hetzner_lb_open_connections",
    focus_range=perfometers.FocusRange(
        perfometers.Closed(0),
        perfometers.Open(100),
    ),
    segments=("hetzner_lb_open_connections",),
)

graph_hetzner_lb_open_connections = graphs.Graph(
    name="hetzner_lb_open_connections",
    title=Title("Load Balancer open connections"),
    simple_lines=("hetzner_lb_open_connections",),
)

graph_hetzner_lb_rates = graphs.Graph(
    name="hetzner_lb_rates",
    title=Title("Load Balancer connection and request rates"),
    simple_lines=(
        "hetzner_lb_connections_per_second",
        "hetzner_lb_requests_per_second",
    ),
    optional=("hetzner_lb_requests_per_second",),
)

graph_hetzner_lb_bandwidth = graphs.Bidirectional(
    name="hetzner_lb_bandwidth",
    title=Title("Load Balancer bandwidth"),
    upper=graphs.Graph(
        name="hetzner_lb_bandwidth_in",
        title=Title("Inbound"),
        compound_lines=("hetzner_lb_bandwidth_in",),
    ),
    lower=graphs.Graph(
        name="hetzner_lb_bandwidth_out",
        title=Title("Outbound"),
        compound_lines=("hetzner_lb_bandwidth_out",),
    ),
)

graph_hetzner_lb_healthy_targets = graphs.Graph(
    name="hetzner_lb_healthy_targets",
    title=Title("Healthy targets"),
    minimal_range=graphs.MinimalRange(0, 1),
    simple_lines=("hetzner_lb_healthy_targets",),
)

graph_hetzner_lb_target_health = graphs.Graph(
    name="hetzner_lb_target_health",
    title=Title("Target health"),
    minimal_range=graphs.MinimalRange(0, 1),
    simple_lines=("hetzner_lb_target_health",),
)
