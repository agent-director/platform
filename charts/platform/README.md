# Platform chart

This chart retains the current release ownership: PostgreSQL, RustFS, application
workloads and the optional Temporal dependency live in the **same platform
release**. The Zalando operator is installed separately through
`charts/postgres-operator`; this is not an infrastructure extraction. The
explicitly deferred Checkov exclusion for `charts/platform` remains unchanged.

## Profiles and staged installation

Use the root lifecycle entrypoint rather than a first-install full Temporal
command:

```sh
make cluster-up CLUSTER_NAME=platform-review-example
make deploy CLUSTER_NAME=platform-review-example PROFILE=kind
```

Choose a new cluster name. Existing clusters require explicit reuse; teardown
only removes a demonstrably owned, explicitly named disposable kind cluster.
`PROFILE=full` selects the pinned Temporal 1.7.0 server chart. It does not imply a
production deployment or configured production Temporal authentication.

- **Bootstrap:** `bootstrap.enabled=true`, `temporal.enabled=false`, and
  `temporalDev.enabled=false` create database resources, public trust material,
  the schema ServiceAccount and RustFS. Dependency-blocked applications are
  suppressed. Bootstrap is only applied on a fresh release; it must not be
  reapplied to remove an already-running full server.
- **Kind:** `values-kind.yaml` enables an in-cluster CLI development server,
  workers and application workloads, while disabling full Temporal,
  `caddyTailscale.enabled`, Langfuse and the OpenShift sandboxed-container
  operator. The CLI uses an exact multi-platform admin-tools **1.32.0** digest,
  UID/GID 1000, a read-only root, bounded writable scratch, resources and an
  explicit `temporal operator cluster health` probe. Custom worker namespaces
  are supplied to `start-dev --namespace`; `default` is created automatically.
  State is disposable.
- **Full:** bootstrap the release, wait for authenticated SQL against the
  pre-created `temporal` and `temporal_visibility` databases, credential
  Secrets, CA ConfigMap and schema ServiceAccount, then upgrade the **same
  release** to a server-only stage. Its upstream post-upgrade hook registers the
  configured worker namespace after the frontend is ready. Only then enable
  workers in another upgrade of that same release; enabling them before the
  post-upgrade hook can deadlock `--wait`. Existing full servers/workers are not
  routinely removed by repeating bootstrap/server-only suppression.
  `bootstrap.prerequisitesReady=true` is only a render guard; the
  lifecycle script earns it through checks. A render is not deployment proof.

Full Temporal uses the v1 persistence layout:
`server.config.persistence.defaultStore`, `visibilityStore`, and
`datastores.<store>.sql` with `pluginName: postgres12`, `connectAddr`,
`databaseName`, `existingSecret`, `createDatabase: false` and
`manageSchema: true`. Obsolete database subchart toggles are not supported.

The upstream chart does not template SQL endpoints, Secret references, volume
references or ServiceAccount values. The non-secret lifecycle ConfigMap supplies
`temporal-values.yaml`, resolved by platform helpers. The lifecycle script writes
that fragment beneath `.cache/deploy` and passes it as an additional values file
on upgrade. Generation and deployment use identical release, namespace and user
inputs. Extra server/schema-tool volumes and mounts are preserved; duplicate or
conflicting projected/dependency-owned names and mount paths are rejected. A
bounded `server-runtime-config` volume makes `/etc/temporal/config` writable for
the upstream entrypoint's generated `docker.yaml` while the container root stays
read-only and the configuration template remains a ConfigMap subPath mount.
The projected full-server prefix is capped at 53 characters so the dependency's
namespace-Job suffix and its frontend address remain consistent for long names. No private
upstream template overrides or modified dependency archives are used.

## Endpoint and credential contract

`platform.resourceName`, `serviceName`, `serviceDNS` and `port` helpers own
resource names, cluster DNS and listener/Service ports. Supported
`<component>.service.port` values drive listeners, Services, environment,
probes and applicable NetworkPolicies. Names are release-derived and honor
`fullnameOverride`; database identity remains `<release>-db`.

The labelled `<fullname>-lifecycle` ConfigMap is the public script interface.
It exports database resources/ports, scoped credential references, CA key,
Temporal/API/RustFS endpoints, schema account, namespace and task queue. It
contains no decoded passwords or private keys. The PostgreSQL listener remains
5432: a different `database.port` is rejected rather than silently ignored.

Application database users are separate owners with no superuser/createdb
flags. Substrate receives the `agent_substrate` role and database by default.
Only public database CA material in `<release>-db-trust` is mounted or copied to
consumers; `<release>-db-tls` and its private key remain in the platform
namespace. OpenShift CA injection uses `service-ca.crt`, mapped to the common
consumer path `/etc/db-trust/ca.crt`.

Workers receive the selected Temporal Service, `TEMPORAL_NAMESPACE`,
`TEMPORAL_TASK_QUEUE`, LiteLLM URL and the existing unified API Service—not a
nonexistent Caddy Service. Their runtime health listener provides `/health` and
`/ready`; chart probes use the configured `workers.health.port`.

## Ingress

Caddy authenticates through its Tailscale plugin and **overwrites**
`Tailscale-User-Login` from authenticated plugin identity, matching the API's
existing identity stub. `/api`, `/api/*`, `/v1` and `/v1/*` are routed to the API
without stripping their paths; remaining requests go to the frontend. The
frontend uses same-origin API paths, not a build-time `NEXT_PUBLIC_API_URL`.

API ingress is restricted to same-release Caddy/workers; the frontend cannot
bypass the authenticated boundary with a forged identity header. Tailscale
control/peer egress excludes private and link-local ranges, with separately
scoped backend and DNS access. A dedicated non-Service health listener is
reachable by kubelet probes. Caddy state uses a PVC with a single replica and
`Recreate`, not Kubernetes Secret persistence.

The existing authentication and orchestration stubs remain stubs; these chart
contracts do not establish a production identity or workflow product.

## Validation and narrow upstream exceptions

`make check-helm-deps` materializes dependencies from committed locks; normal
builds do not refresh versions. `make lint-helm` and `make check-schema` cover
current, kind, bootstrap and composed full render contracts. Unit contracts
render with Helm and inspect documents/certificates in Python, including
non-default names, namespaces and ports; they do not invoke an external
certificate executable, Docker or Caddy. Actual custom
Caddy binary/plugin checks and container/cluster health belong to integration,
not unit tests.

The strengthened KubeLinter baseline remains enabled. Upstream schema completion
Jobs and one-shot Helm test Pods have component-specific, explained probe
exceptions. The upstream test template exposes no securityContext override;
its exact admin-tools image declares non-root UID 1000, and this limited test
Pod is explicitly documented separately from hardened server/schema/dev
workloads. The full internal Temporal worker probes its actual membership
listener, not a nonexistent workflow RPC port. Rendering and linting do not
prove full Temporal database migrations or live deployment succeeded.
