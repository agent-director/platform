# Postgres operator bootstrap wrapper

A thin wrapper pins the existing independently installed Zalando Postgres
operator dependency to **2.0.3** with a committed `Chart.lock`. It is installed
as release `postgres-operator` in the platform namespace by default; the
platform PostgreSQL CR and backing resources remain in `charts/platform`.
This does not extract infrastructure or bypass its deferred Checkov scope.

Normal setup uses `scripts/helm_dependencies.py build` and existing locks.
Only an intentional provider upgrade may update the declared exact version and
lock. The CRD/schema updater reads the materialized locked archive
`charts/postgres-operator/charts/postgres-operator-2.0.3.tgz`, never an unrelated
upstream branch.

Version-matched CRDs are packaged in `crds/` and applied before operator startup;
wait for `postgresqls.acid.zalan.do` to become Established and the operator
Deployment to roll out before creating database resources. Upstream also
packages the same CRDs; Helm skips existing CRDs, and an `--include-crds` render
may list identical parent/child definitions. Controller CRD registration is
explicitly disabled so it does not independently replace the selected inputs.

The wrapper supplies bounded resources, non-root/read-only/capability-drop
security and database readiness probes. Upstream operator readiness is the real
`/readyz` listener on 8080. Its chart omits a declared containerPort and offers
no liveness override or owning-resource annotation interface; security scanning
uses `.kube-linter-operator.yaml` **only for the operator wrapper render**, with
exactly `no-liveness-probe` and `readiness-port` excluded. A dependency-version
guard requires review when the 2.0.3 pin changes. All other defaults remain
enabled; platform/kind/full/substrate profiles use the stronger standard policy.
This is a documented third-party limitation, not an upstream health fix, global
baseline exclusion or edited archive.

Root `make scan-iac` scans the operator dependency rather than skipping its
`charts/` directory (which prevents Trivy from rendering the wrapper). The
explicit `.trivyignore.yaml` matches only exact dependency template paths and
checks: required credential-Secret management (`KSV-0041`), filesystem-resize
`pods/exec` (`KSV-0053`), controller/Patroni Service management (`KSV-0056`), and
the absent pod-level security context (`KSV-0118`). Upstream 2.0.3 has no
pod-security-context value or template hook; its only container is explicitly
hardened by this wrapper's supported `securityContext` values. Other security
checks still enforce that Deployment. The exceptions require re-review when
the operator 2.0.3 or Trivy 0.75.0 pin changes; native controls demonstrate
scanned resources, failure without exceptions, identical checks failing on
first-party lookalike templates, and other checks failing on exempted files.

The database pods use the selected Spilo image's postgres UID **101** and GID
**103**, including pod `fsGroup: 103`. The operator mounts TLS secrets with mode
`0640`; without the filesystem group, PostgreSQL cannot read the root-owned
certificate/key. These settings belong to the operator's `configKubernetes`,
not the controller container's `securityContext`.

Operator readiness is not database readiness. The staged platform lifecycle
checks the generated scoped credentials and executes authenticated SQL against
each required database, waits for public CA material and the pre-existing
schema account, then upgrades the same platform release to full runtime.
