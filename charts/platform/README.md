# Platform chart

This chart retains the current release ownership: PostgreSQL, RustFS, application
workloads and Temporal (first-party templates, or the in-cluster CLI dev server
under `PROFILE=kind`) live in the **same platform release**. The
Zalando `postgres-operator` is installed directly as a standalone release from
its upstream Helm repository (`charts/postgres-operator-values.yaml`).
`charts/platform/values.schema.json` strictly validates all chart inputs before rendering, and Checkov scans both default and `values-kind.yaml` profiles with zero `skip-path` exclusions.

## Profiles and installation

```sh
make cluster-up
make deploy PROFILE=kind
```

`PROFILE=full` (the chart's default in `values.yaml`) deploys first-party
`charts/platform/templates/temporal.yaml` templates (`temporalio/server:1.32.0` with
`passwordCommand` and non-hook `temporalio/admin-tools:1.32.0` schema/namespace Jobs)
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

Full-profile Temporal objects are named `<base>-temporal-<role>` (capped at 45 characters so the longest headless Service name stays within 63). A base that already contains `temporal` is reused as is, so a release named `temporal` renders `temporal-frontend`, unless that would equal the UI fullname (a `fullnameOverride`, or a release that also contains `platform`), in which case `-temporal` is appended. `temporal.nameOverride` (truncated to 20 characters in resource names) and `temporal.fullnameOverride` are honored, and the chart fails to render when Temporal names would collide with platform names. Upgrading a release whose Temporal names change (releases of 37 or more characters, a `temporal.nameOverride` over 20 characters, or a release or `fullnameOverride` that contains `temporal` and equals the UI fullname, such as `temporal-platform`) recreates the Temporal Deployments and Services, causing a short Temporal outage with no data loss because state lives in PostgreSQL.

Application database users are separate owners with no superuser/createdb
flags. Substrate receives the `agent_substrate` role and database by default.
Only public database CA material in `<release>-db-trust` is mounted or copied to
consumers; `<release>-db-tls` and its private key remain in the platform
namespace.

## Network policies

Each enabled workload has a pod-label-scoped `NetworkPolicy` (`templates/networkpolicies.yaml`, plus `workers.yaml` and `rustfs.yaml`) selecting it through `platform.selectorLabels` (full-profile Temporal pods, including its schema and namespace Jobs, use `platform.temporalSelectorLabels`), with both `Ingress` and `Egress` policy types. No policy uses a namespace-wide `podSelector`, so the `postgres-operator` and Spilo database pods are not selected by any chart policy; they appear only as egress destinations (`cluster-name: <release>-db`).

- Ingress: `frontend` accepts only `caddy-tailscale`; `unified-api` accepts only `caddy-tailscale` and `workers`; `litellm` accepts `unified-api` and `workers`; `langfuse` accepts `litellm`, `workers` and `unified-api`; Temporal (the `PROFILE=kind` dev server or the full-profile server) accepts `workers` and `unified-api`, plus intra-Temporal service ports in the full profile. `workers` and `caddy-tailscale` accept no ingress, and `rustfs` accepts same-release pods and substrate `ateapi`.
- Egress: kube-system DNS plus named peers only. `caddy-tailscale` reaches `frontend` and `unified-api`; `unified-api` reaches `litellm`, `langfuse`, Temporal and the database; `litellm` reaches `langfuse`; `langfuse` reaches the database; Temporal (full profile) reaches the database and its own peers; `workers` reach Temporal, `unified-api`, `litellm`, `langfuse` and substrate `ateapi`; `frontend`, the Temporal dev server and `rustfs` have no egress.
- Internet: only `caddy-tailscale`, `unified-api` and `litellm` may reach it (TCP 443; `caddy-tailscale` also UDP 443/3478 and direct-peer UDP), and every such rule excludes RFC 1918 and link-local ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`).

The Temporal Deployments and Jobs accept `nodeSelector`, `tolerations`, `affinity` and `podAnnotations` under `temporal.server` and `temporal.schema`.

## Ingress

Caddy authenticates through its Tailscale plugin and **overwrites**
`Tailscale-User-Login` and `Tailscale-Tailnet` from authenticated plugin identity,
matching the API's identity header and RFC 6648 naming conventions. `/api`, `/api/*`,
`/v1` and `/v1/*` are routed to the API without stripping their paths; remaining
requests go to the frontend.
