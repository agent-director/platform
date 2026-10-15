# Platform chart

This chart retains the current release ownership: PostgreSQL, RustFS, application
workloads and the Temporal dependency live in the **same platform release**. The
Zalando `postgres-operator` is installed directly as a standalone release from
its upstream Helm repository (`charts/platform/postgres-operator-values.yaml`).
The explicitly deferred Checkov exclusion for `charts/platform` remains unchanged.

## Profiles and installation

```sh
make cluster-up
make deploy PROFILE=kind
```

`PROFILE=full` (the chart's default in `values.yaml`) selects the pinned Temporal
`1.7.0` server chart with non-hook schema and namespace setup Jobs (`useHelmHooks: false`)
so single-pass Helm convergence succeeds alongside the `postgresql` CR.
`PROFILE=kind` (`values-kind.yaml`) switches to the lightweight in-cluster Temporal
CLI development server (`temporalDev.enabled: true`, `temporal.enabled: false`) while
keeping application workloads, workers, and Langfuse enabled by default.

## Endpoint and credential contract

`platform.resourceName`, `serviceName`, `serviceDNS` and `port` helpers own
resource names, cluster DNS and listener/Service ports. Supported
`<component>.service.port` values drive listeners, Services, environment,
probes and applicable NetworkPolicies. Names are release-derived and honor
`fullnameOverride`; database identity remains `<release>-db`.

Application database users are separate owners with no superuser/createdb
flags. Substrate receives the `agent_substrate` role and database by default.
Only public database CA material in `<release>-db-trust` is mounted or copied to
consumers; `<release>-db-tls` and its private key remain in the platform
namespace.

## Ingress

Caddy authenticates through its Tailscale plugin and **overwrites**
`Tailscale-User-Login` and `Tailscale-Tailnet` from authenticated plugin identity,
matching the API's identity header and RFC 6648 naming conventions. `/api`, `/api/*`,
`/v1` and `/v1/*` are routed to the API without stripping their paths; remaining
requests go to the frontend.
