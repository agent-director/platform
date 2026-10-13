# Agent substrate chart

This chart packages the existing v0.3.0 substrate control-plane workloads and
version-coupled CRDs. It does not add the planned local-runner implementation or
replace the existing identity/orchestration stubs with production features.

The root staged lifecycle installs this separate release after platform backing
services are ready. It transports chart-owned database/RustFS endpoints through
an allowlisted values file beneath `.cache/deploy`, copies only the scoped
substrate role credential and RustFS credential Secrets, and copies the **public
CA ConfigMap**. It never copies the platform database TLS private key.

## Service and TLS contract

The API Deployment and Service use `<fullname>-ateapi`. `ateapi.service.port`
drives the gRPC listener, declared container port, Service target, TCP probes,
controller connection and NetworkPolicies. `fullnameOverride`, release and
namespace changes are reflected in certificate SANs and the controller's
`--ateapi-server-name`. The staged lifecycle derives the effective substrate
Service port before any mutation and injects it into platform worker egress;
configure custom ports through `SUBSTRATE_VALUES`, not an independent
`workers.substratePort` CLI override. The controller trusts `/etc/tls/ca.crt`,
not a private credential bundle as its CA file.

For local self-signed mode, the chart generates/reuses `<fullname>-tls` (or
`tls.secretName`) with the upstream-required serving/client bundle. The v0.3.0
loader requires a **PKCS#8 `PRIVATE KEY`** block followed by the leaf and CA
certificates; legacy RSA/EC PEM key formats do not work. Chart-generated leaf
keys use Ed25519/PKCS#8. Compatible lookup credentials are preserved, while
legacy chart-owned keys are replaced; pod-template checksums roll the API and
controller when chart-managed TLS material changes. External bundle preparation
must supply the same key-first format. The dummy JWT configuration remains an
existing stub. OpenShift serving-cert mode requires
`tls.existingBundleSecret` naming an upstream-compatible, preassembled Secret
with nonempty `bundle.pem`, `ca.crt`, `auth.yaml` and `empty.json`. A plain
OpenShift `tls.crt`/`tls.key` Secret is rejected as unsupported configuration.
The chart checks required keys when cluster lookup is available and mounts
explicit required Secret items, so missing Secrets/keys cannot start the pods.
Offline positive renders prove reference selection, not existence or certificate
contents. The operator-generated serving Secret and the preassembled credential
bundle remain distinct inputs; external preparation/renewal owns their coherence.
No init-container assembly, secret controller or new identity architecture is
introduced. Do not treat this mode as proven by local render tests.

## Database and network boundaries

`database.host`, `port`, `name`, `user`, `credentialSecret`, `trustConfigMap`
and `trustKey` are explicit inputs. Defaults use the `agent_substrate` role and
database, not an administrator or the `postgres` database. Connections verify
the server against `/etc/db-trust/ca.crt` with `sslmode=verify-full`. The URI
passed to the upstream process excludes its password because upstream logs flag
values at startup. Pinned pgx reads the existing Secret-backed `PGPASSWORD`
environment variable directly; no password is placed in the logged URI/argv.

Default-deny policies admit controller/API and platform-worker traffic only on
the configured API port. Egress is limited to DNS, selected database/RustFS peers
and the discovered or explicitly supplied API-server CIDRs. Offline renders do
not create an unrestricted API-server rule when discovery returns no peers.

The v0.3.0 controller's Secret cache is scoped to `POD_NAMESPACE` and a
`metadata.name=egress-mitm-ca-pool` field selector. A namespaced, resource-named
Role supplies that access; the controller no longer receives cluster-wide
Secret reads. Existing cluster-scoped pod/CRD watchers retain their documented
upstream requirements.

## Health and validation limitations

The API's TLS TCP probes confirm that its configured listener accepts
connections; they do not authenticate a gRPC operation. The pinned v0.3.0
controller adds health checks but does **not** enable the controller-runtime
health listener or expose a health-bind flag. Invented TCP 8080 probes were
removed. Only that Deployment carries specifically justified
`no-readiness-probe` and `no-liveness-probe` KubeLinter exceptions; Kubernetes
still supervises process exit. A successful controller rollout is therefore not
proof of application-level health.

Unit chart contracts independently check non-default Service/port/namespace,
certificate SAN, TLS server name, scoped credentials/public trust and RBAC.
`helm lint`/schema/security render validation do not prove live substrate
integration or production authentication.
