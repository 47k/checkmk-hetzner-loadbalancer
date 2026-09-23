# checkmk-hetzner-loadbalancer

Checkmk 2.5 extension for monitoring Hetzner Cloud Load Balancers through the official Cloud API.

The extension runs entirely on the Checkmk server. It discovers every Load Balancer returned for a project, its services and targets, Hetzner's target health assessment, and current performance values.

## Compatibility

- Checkmk 2.5.x
- Checkmk Raw and Enterprise editions
- Hetzner Cloud API `https://api.hetzner.cloud/v1`

Compatibility with older Checkmk versions is not claimed.

## Monitored services

The extension creates:

- `Hetzner Load Balancer API`: API/collection status. This service remains present when the project has no Load Balancers or collection fails.
- `Hetzner Load Balancer <id>`: one informational configuration/status service per Load Balancer. Its output includes name, type, location, algorithm, public networking, addresses, services, and target count.
- `Hetzner LB Target <item>`: one service per API-provided target health entry. The stable item contains the Load Balancer ID, matched protocol and ports, target type, and target identity.
- `Hetzner LB Performance <id>`: one service per Load Balancer when metric collection is enabled.

Load Balancer IDs are part of item names so identically named Load Balancers and targets cannot collide. Target items additionally contain the frontend service identity, which keeps multiple services on the same target distinct.
Server targets use the stable item suffix `server:<server-id>`; their IP address is informational and does not affect discovery identity. The general service omits operational status when the API does not supply one and shows the balancing algorithm instead.

### Target health versus external availability

Target health is the state reported by Hetzner's Load Balancer infrastructure. It is not an end-to-end check from the Checkmk server and does not replace an active SMTP, HTTP, HTTPS, or TCP check against the public service.

For example, an external SMTP check can be OK while one of two targets is CRIT. The service remains reachable, but redundancy is degraded. Conversely, all Hetzner targets can be healthy while an external protocol check fails.

Health output distinguishes the frontend and backend, for example:

```text
OK - Health: healthy, Target: 192.0.2.10, frontend TCP/25 -> backend TCP/26
```

The `Target` field is omitted when no address is available. If the configured health-check protocol or port differs from the destination, it is shown separately.

Known `healthy` states are OK and `unhealthy` states are CRIT. Future/unknown API states are UNKNOWN. Health entries are associated with configured services by `listen_port`; unmatched entries remain visible without invented frontend/backend configuration. A target for which the API provides no health entry does not create a target-health service.

## Metrics

The performance service can emit:

| Checkmk metric | Hetzner series | Unit |
| --- | --- | --- |
| `hetzner_lb_open_connections` | `open_connections` | connections |
| `hetzner_lb_connections_per_second` | `connections_per_second` | connections/s |
| `hetzner_lb_requests_per_second` | `requests_per_second` | requests/s |
| `hetzner_lb_bandwidth_in` | `bandwidth.in` | bytes/s |
| `hetzner_lb_bandwidth_out` | `bandwidth.out` | bytes/s |

Hetzner's [Cloud API reference](https://docs.hetzner.cloud/reference/cloud#tag/load-balancers/GET/load_balancers/{id}/metrics) defines the bandwidth series as bytes per second. Graph definitions use Checkmk's public graphing API and the `B/s` unit.

`requests_per_second` and any other unavailable/null series are omitted. A null value is never converted to zero.

The metrics API is queried for a fixed five-minute window at 60-second resolution. The agent selects the newest valid value in each series, then accepts it only when it is no more than 120 seconds older than the query end. Freshness is evaluated independently for every metric. Null, missing, and stale values are not emitted as perfdata; their availability is reported in service details. The agent does not average samples or forward raw history; Checkmk stores the resulting perfdata history itself.

The open-connections metric also has a Perf-O-Meter for immediate visibility in the service overview.

Target health adds two state-history metrics without changing check states:

- `hetzner_lb_healthy_targets` is emitted on the general Load Balancer service and counts healthy API-provided target-health associations. It is emitted only when every discovered association for that Load Balancer has an explicit `healthy` or `unhealthy` state.
- `hetzner_lb_target_health` is emitted on each target-health service as `1` for explicitly healthy and `0` for explicitly unhealthy. Because each target-health service item contains the Load Balancer, frontend service, ports, and stable target identity, the same target on multiple services has independent metric history.

The public Checkmk 2.5 graphing API uses statically registered metric names and does not provide wildcard graph definitions for arbitrary target-derived names. Keeping the binary metric on each already-unique target-health service therefore provides a separate 0..1 history graph per association without unsupported dynamic registration. Unknown, missing, or unsupported health states emit no binary metric and cannot be mistaken for an outage.

Result caching occurs after that validation. A fresh cached result can therefore contain a sample that was at most 120 seconds old relative to its metrics query end, plus up to the configured cache TTL and the elapsed collection time before the cache is written. With the default 60-second TTL, its wall-clock age can approach 180 seconds plus that collection overhead immediately before cache expiry. A longer custom TTL increases that bound. During stale fallback cached metrics can be older, but only until the configured maximum stale age. They remain the originally collected samples; only the central API service becomes WARN, while the performance service retains its metric-specific state and identifies the stale data source in its details.

## API token

Create a read-only API token in the Hetzner Cloud Console for the project containing the Load Balancers. Read permission is sufficient. The special-agent rule stores the token with Checkmk's password handling and passes a password-store reference to the agent; the clear token is not included in agent output or normal error messages.

Endpoints used:

```text
GET /load_balancers
GET /load_balancers/{id}/metrics
Authorization: Bearer <API_TOKEN>
```

The list endpoint is paginated. Metric errors are isolated per Load Balancer, so a failed metrics request does not suppress inventory or target-health monitoring.

## Installation

Install a built package as the Checkmk site user:

```bash
mkp add hetzner_loadbalancer-0.1.0.mkp
mkp enable hetzner_loadbalancer
cmk -R
```

For a manual development install, copy the plugin tree to the site's local Python extension directory:

```bash
cp -a cmk_addons/plugins/hetzner_loadbalancer ~/local/lib/python3/cmk_addons/plugins/
cmk -R
```

## Configuration

1. In Setup, open **Agents > Other integrations > Applications**.
2. Add a **Hetzner Cloud Load Balancers** rule for the Checkmk host that represents the project.
3. Select or create the API token in the password field.
4. Keep the official API URL unless a compatible endpoint is intentionally used.
5. Adjust the HTTP timeout if needed.
6. Leave performance metrics enabled unless only configuration and target health are wanted.
7. Adjust result caching if needed. It is enabled by default with a 60-second TTL, a 900-second maximum stale age, and stale fallback enabled.
8. Run service discovery on the host.

One rule/token monitors all Load Balancers in that Hetzner Cloud project. No Load Balancer ID, address, service, or port is configured in Checkmk.

### Result cache

The special agent keeps a site-local result cache under `var/check_mk/cache/hetzner_loadbalancer/`. The Checkmk server-side call passes the rule's cache policy to the special agent; caching wraps collection and does not change the Hetzner API requests or metric sample selection.

Default behavior:

- Result caching is enabled.
- The TTL is 60 seconds and remains configurable.
- The maximum stale age is 900 seconds and remains configurable.
- Use of stale cache after a collection error is enabled.

The 60-second TTL approximately matches a normal one-minute Checkmk monitoring interval. Its main purpose is to avoid duplicate API requests caused by additional executions within the same monitoring cycle. It is intentionally much shorter than the Storage Box integration's one-hour default because target health and Load Balancer performance are operational, near-real-time signals. An enabled cache requires a TTL of at least one second; use the explicit **Disabled** selection instead of setting the TTL to zero.

After the TTL expires, the agent attempts a normal API refresh. If that refresh fails and stale fallback is enabled, the last usable result can bridge a short outage only while its age is no greater than the configured maximum stale age. Maximum stale age must be at least the TTL when stale fallback is enabled. Once the maximum is exceeded, cached data is not used and the normal collection failure is reported.

Stale fallback is not silent. Only the central `Hetzner Load Balancer API` service becomes WARN and reports cache age, TTL, maximum stale age, and the sanitized refresh error. General Load Balancer, target-health, and performance services retain their domain-specific states; their details identify that the data source is stale cache and show its age.

Cache entries are separated by API URL, token hash, timeout, and performance-metrics setting. The clear API token is not stored in cache metadata or output.

## Error handling

- Authentication, HTTP, connection, timeout, malformed JSON, response-shape, and pagination errors appear as UNKNOWN on the API service.
- An empty Load Balancer list is OK and explicitly reported.
- A metrics failure appears as UNKNOWN only on the affected performance service.
- Missing, null, and stale metric series are omitted from perfdata and reported in performance details.
- Missing optional target and service fields are represented defensively and do not crash parsing or checking.
- Unknown target types receive a deterministic fallback identity when the API supplies a health entry.
- Stale cache fallback produces a WARN only on the central API service. Domain-service details identify stale cached data without overriding their actual state.

The agent sanitizes the API token if an exception includes it. Do not run the agent with shell tracing enabled, because direct manual invocation necessarily puts a literal token in the invoking shell's command line.

## Troubleshooting

Inspect the data source without printing a token:

```bash
cmk -v --debug --detect-plugins=hetzner_loadbalancer <host-name>
cmk -IIv <host-name>
cmk -nv <host-name>
```

Common causes of UNKNOWN are an expired/wrong-project token, API reachability, TLS interception, or a per-Load-Balancer metrics error. The API service distinguishes collection errors from a valid empty project.

To test the API independently, place the token in an environment variable rather than writing it into a script:

```bash
curl -sS -H "Authorization: Bearer $HETZNER_API_TOKEN" \
  -H "Accept: application/json" \
  https://api.hetzner.cloud/v1/load_balancers
```

## Development and packaging

Run the automated tests and syntax checks from the repository root:

```bash
python -m pytest -q tests
python -m py_compile \
  cmk_addons/plugins/hetzner_loadbalancer/agent_based/hetzner_loadbalancer.py \
  cmk_addons/plugins/hetzner_loadbalancer/graphing/hetzner_loadbalancer.py \
  cmk_addons/plugins/hetzner_loadbalancer/rulesets/hetzner_loadbalancer.py \
  cmk_addons/plugins/hetzner_loadbalancer/server_side_calls/hetzner_loadbalancer.py \
  cmk_addons/plugins/hetzner_loadbalancer/libexec/agent_hetzner_loadbalancer
```

This workspace's deployment helper validates `package.yml`, installs the source tree into the selected development site, generates Checkmk's native manifest, and can build the MKP. Run it from the workspace root, choose **Deploy & Create MKP**, then select this plugin:

```bash
/srv/checkmk-dev/deploy.sh
```

Inspect the resulting package before distribution:

```bash
mkp inspect hetzner_loadbalancer-0.1.0.mkp
```

All fixtures use documentation-only IP ranges and fictional IDs. No live API token is required for the unit suite.

## Limitations

- Thresholds are intentionally not applied to Load Balancer configuration or performance values; no generally valid capacity limits can be inferred from static properties.
- Target health is limited to API-provided health entries. Targets without a health array remain counted in the general LB service but do not create fabricated health services.
- The extension currently monitors all Load Balancers accessible to the configured project token; it has no per-ID allow-list.
