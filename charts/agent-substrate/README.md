# Agent substrate chart

This chart packages the upstream `v0.4.0` Agent Substrate (`agent-substrate/substrate`)
control-plane workloads (`ateapi` and `atecontroller`) and version-coupled CRDs.

## Service and TLS contract

The API Deployment and Service use `<fullname>-ateapi`. `ateapi.service.port`
drives the gRPC listener, declared container port, Service target, TCP probes,
controller connection and NetworkPolicies. `fullnameOverride`, release and
namespace changes are reflected in certificate SANs and the controller's
`--ateapi-server-name`.

For local self-signed mode, the chart generates/reuses `<fullname>-tls` (or
`tls.secretName`) with an Ed25519/PKCS#8 `PRIVATE KEY` followed by the leaf and CA
certificates (`bundle.pem`). Pod-template checksum annotations roll both `ateapi`
and `atecontroller` when chart-managed TLS material changes, and `ateapi` passes
`--pod-identity-ca-certs=/etc/tls/ca.crt` (live-reloaded in `v0.4.0`).

## Database and network boundaries

`charts/agent-substrate/values.schema.json` strictly validates all chart inputs, types, port bounds, and DNS labels before rendering. `database.host`, `port`, `name`, `user`, `credentialSecret`, `trustConfigMap`
and `trustKey` are explicit inputs. Defaults use the `agent_substrate` role and
database. Connections verify the server against `/etc/db-trust/ca.crt` with
`sslmode=verify-full` and pass `--postgres-read-write-connection-string`,
`--postgres-owner-connection-string`, `--postgres-read-write-role`, and
`--postgres-owner-role`. The URI excludes its password; `pgx` reads the
Secret-backed `PGPASSWORD` environment variable directly.

## Health probes

- `ateapi` uses TCP startup, readiness, and liveness probes on its configured gRPC port.
- `atecontroller` exposes its controller-runtime `/metrics` HTTP listener on port `8080` (`metrics`), which is probed directly without KubeLinter or Checkov probe exemptions.
