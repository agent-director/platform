#!/bin/bash
# Shared local lifecycle contract. No command uses the caller's ambient context.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
run() { mise exec -- "$@"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# renovate: datasource=github-tags depName=zalando/postgres-operator extractVersion=^v?(?<version>.*)$
POSTGRES_OPERATOR_VERSION=2.0.3
# renovate: datasource=docker depName=kindest/node
KIND_NODE_IMAGE="kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"

CLUSTER_NAME=${CLUSTER_NAME:-platform-dev}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-$CLUSTER_NAME}
if [[ -n "$CLUSTER_NAME" ]]; then
    # Never let kind update the user's global kubeconfig/current context.
    export KUBECONFIG="$ROOT/.cache/clusters/$CLUSTER_NAME/kubeconfig"
fi
NAMESPACE=${NAMESPACE:-platform}
RELEASE_NAME=${RELEASE_NAME:-platform}
SUBSTRATE_NAMESPACE=${SUBSTRATE_NAMESPACE:-agent-substrate}
SUBSTRATE_RELEASE_NAME=${SUBSTRATE_RELEASE_NAME:-agent-substrate}
PROVIDER_NAMESPACE=${PROVIDER_NAMESPACE:-$NAMESPACE}
PROVIDER_RELEASE_NAME=${PROVIDER_RELEASE_NAME:-postgres-operator}
PROFILE=${PROFILE:-kind}
IMAGE_TAG=${IMAGE_TAG:-local}
WAIT_TIMEOUT=${WAIT_TIMEOUT:-600s}
READINESS_ATTEMPTS=${READINESS_ATTEMPTS:-120}
READINESS_INTERVAL=${READINESS_INTERVAL:-2}
KUBE_REQUEST_TIMEOUT=${KUBE_REQUEST_TIMEOUT:-15s}
SQL_PROBE_TIMEOUT=${SQL_PROBE_TIMEOUT:-15}

PRIVATE_VALUES_FILE=
SUBSTRATE_VALUES_FILE=
KIND_VERIFY_CONFIG=
cleanup_private_values() {
    local status=$?
    trap - EXIT INT TERM
    if [[ -n "$PRIVATE_VALUES_FILE" ]]; then rm -f "$PRIVATE_VALUES_FILE"; fi
    if [[ -n "$SUBSTRATE_VALUES_FILE" ]]; then rm -f "$SUBSTRATE_VALUES_FILE"; fi
    if [[ -n "$KIND_VERIFY_CONFIG" ]]; then rm -f "$KIND_VERIFY_CONFIG"; fi
    exit "$status"
}
trap cleanup_private_values EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

load_application_settings() {
    # Parse only literal application settings. .env is data, never executable shell,
    # and cannot replace the caller's already-established lifecycle destination.
    APPLICATION_SETTINGS=$(run uv run --no-project python -c '
import json, os, pathlib, shlex, sys
allowed = ("GH_USER", "GH_PAT", "TAILSCALE_AUTH_KEY", "TAILSCALE_DOMAIN", "LANGFUSE_NEXTAUTH_SECRET", "LANGFUSE_SALT")
protected = {"CLUSTER_NAME", "KUBE_CONTEXT", "KUBECONFIG", "NAMESPACE", "RELEASE_NAME", "SUBSTRATE_NAMESPACE", "SUBSTRATE_RELEASE_NAME", "PROVIDER_NAMESPACE", "PROVIDER_RELEASE_NAME", "PROFILE", "KUBE_API_URL"}
values = {}
path = pathlib.Path(sys.argv[1])
if path.exists():
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if key in protected:
            raise ValueError("Lifecycle destination settings are forbidden in .env: " + key)
        if key not in allowed:
            continue
        if not separator:
            raise ValueError("Malformed application setting: " + key)
        words = shlex.split(value, comments=True)
        if len(words) > 1:
            raise ValueError("Application setting must be one literal value: " + key)
        values[key] = words[0] if words else ""
json.dump({key: os.environ.get(key, values.get(key, "")) for key in allowed}, sys.stdout)
' "$ROOT/.env")
}

validate_value_option() {
    local expression="$1"
    local reserved='(^|,)[[:space:]]*(temporal|temporalDev|workers)([.]enabled)?[[:space:]]*='
    [[ ! "$expression" =~ $reserved ]] || fail 'PROFILE owns Temporal/worker switches; conflicting Helm value override rejected.'
    local substrate_port='(^|,)[[:space:]]*workers[.]substratePort[[:space:]]*='
    [[ ! "$expression" =~ $substrate_port ]] || fail 'SUBSTRATE_VALUES owns the substrate API port; conflicting workers.substratePort override rejected.'
}

validate_helm_options() {
    local index=0 option
    while [[ "$index" -lt "${#HELM_OPTIONS[@]}" ]]; do
        option=${HELM_OPTIONS[$index]}
        case "$option" in
            -f|--values|--set|--set-string|--set-json|--set-file)
                index=$((index + 1))
                [[ "$index" -lt "${#HELM_OPTIONS[@]}" && -n "${HELM_OPTIONS[$index]}" ]] || fail "Missing value for $option."
                if [[ "$option" != -f && "$option" != --values ]]; then validate_value_option "${HELM_OPTIONS[$index]}"; fi
                ;;
            --values=*) ;;
            --set=*|--set-string=*|--set-json=*|--set-file=*) validate_value_option "${option#*=}" ;;
            *) fail "Only Helm chart-value options are allowed; destination/transport option rejected: $option" ;;
        esac
        index=$((index + 1))
    done
}

usage() {
    printf '%s\n' \
        'Usage: tools/scripts/deploy.sh cluster-up|check-cluster|deploy [Helm options after --]' \
        'Set CLUSTER_NAME (defaults to platform-dev), NAMESPACE, RELEASE_NAME and PROFILE=kind|full.' \
        'Existing clusters require REUSE_CLUSTER=true and are not newly owned.' \
        'DEPLOY_VALUES and SUBSTRATE_VALUES optionally name Helm values files.'
}

validate_name() {
    [[ "$2" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#2} -le 63 ]] || fail "$1 must be a DNS label."
}

validate_contract() {
    [[ -n "$KUBE_CONTEXT" ]] || fail 'Set CLUSTER_NAME or KUBE_CONTEXT explicitly.'
    if [[ -n "$CLUSTER_NAME" ]]; then
        validate_name CLUSTER_NAME "$CLUSTER_NAME"
        [[ "$KUBE_CONTEXT" == "kind-$CLUSTER_NAME" ]] || fail 'CLUSTER_NAME and KUBE_CONTEXT disagree.'
    fi
    for name in NAMESPACE RELEASE_NAME SUBSTRATE_NAMESPACE SUBSTRATE_RELEASE_NAME PROVIDER_NAMESPACE PROVIDER_RELEASE_NAME; do
        validate_name "$name" "${!name}"
    done
    [[ "$PROFILE" == kind || "$PROFILE" == full ]] || fail 'PROFILE must be kind or full.'
    [[ "$READINESS_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail 'READINESS_ATTEMPTS must be positive.'
    [[ "$SQL_PROBE_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail 'SQL_PROBE_TIMEOUT must be positive.'
}

kube() { run kubectl --context "$KUBE_CONTEXT" --request-timeout="$KUBE_REQUEST_TIMEOUT" "$@"; }

bounded_sql_exec() {
    run uv run --no-project python -c '
import subprocess, sys
try:
    subprocess.run(sys.argv[2:], input=sys.stdin.buffer.read(), timeout=float(sys.argv[1]), check=True)
except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
    raise SystemExit(1)
' "$SQL_PROBE_TIMEOUT" mise exec -- kubectl --context "$KUBE_CONTEXT" --request-timeout="$KUBE_REQUEST_TIMEOUT" "$@"
}

helm_context() {
    if [[ -n "${KUBECONFIG:-}" ]]; then
        run helm "$@" --kube-context "$KUBE_CONTEXT" --kubeconfig "$KUBECONFIG"
    else
        run helm "$@" --kube-context "$KUBE_CONTEXT"
    fi
}

check_cluster() {
    validate_contract
    if [[ -n "$CLUSTER_NAME" && ! -s "$KUBECONFIG" ]]; then
        fail "Kubeconfig not found at $KUBECONFIG; run 'make cluster-up' (or 'make cluster-up REUSE_CLUSTER=true' for an existing cluster)."
    fi
    local server uid fresh_server fresh_uid
    server=$(kube config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
    [[ -n "$server" ]] || fail "No API server found for context $KUBE_CONTEXT."
    [[ "$server" =~ ^https://127\.0\.0\.1:[0-9]+$ ]] || fail "Cluster API server must bind strictly to 127.0.0.1 (got $server)."
    if [[ -n "${KUBE_API_URL:-}" && "$KUBE_API_URL" != "$server" ]]; then
        fail 'KUBE_API_URL does not match the explicitly selected context.'
    fi
    uid=$(kube get namespace kube-system -o 'jsonpath={.metadata.uid}')
    [[ -n "$uid" ]] || fail 'Selected cluster identity is unavailable.'
    if [[ -n "$CLUSTER_NAME" ]]; then
        mkdir -p "$ROOT/.cache/clusters/$CLUSTER_NAME"
        KIND_VERIFY_CONFIG=$(umask 077; mktemp "$ROOT/.cache/clusters/$CLUSTER_NAME/verify.XXXXXXXX")
        run kind get kubeconfig --name "$CLUSTER_NAME" > "$KIND_VERIFY_CONFIG"
        fresh_uid=$(run kubectl --kubeconfig "$KIND_VERIFY_CONFIG" --context "$KUBE_CONTEXT" --request-timeout="$KUBE_REQUEST_TIMEOUT" get namespace kube-system -o 'jsonpath={.metadata.uid}')
        fresh_server=$(run kubectl --kubeconfig "$KIND_VERIFY_CONFIG" --context "$KUBE_CONTEXT" config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
        [[ "$fresh_uid" == "$uid" && "$fresh_server" == "$server" ]] || fail 'Cached context and selected kind backend identify different clusters; refusing mutation.'
        rm -f "$KIND_VERIFY_CONFIG"
        KIND_VERIFY_CONFIG=
    fi
}

cluster_up() {
    [[ -n "$CLUSTER_NAME" ]] || fail 'cluster-up requires an explicitly named CLUSTER_NAME.'
    validate_contract
    local existing name marker uid server
    existing=$(run kind get clusters)
    while IFS= read -r name; do
        if [[ "$name" == "$CLUSTER_NAME" ]]; then
            [[ "${REUSE_CLUSTER:-false}" == true ]] || fail "Cluster $CLUSTER_NAME already exists; use REUSE_CLUSTER=true to reuse without claiming ownership."
            mkdir -p "$ROOT/.cache/clusters/$CLUSTER_NAME"
            run kind export kubeconfig --name "$CLUSTER_NAME" --kubeconfig "$KUBECONFIG"
            check_cluster
            printf 'Reusing %s; no creation ownership marker written.\n' "$CLUSTER_NAME"
            return
        fi
    done <<< "$existing"
    mkdir -p "$ROOT/.cache/kind" "$ROOT/.cache/clusters/$CLUSTER_NAME"
    marker="$ROOT/.cache/kind/$CLUSTER_NAME.json"
    [[ ! -e "$marker" ]] || fail "A stale ownership marker exists at $marker; inspect it before creating a replacement cluster."
    local config="$ROOT/.cache/kind/$CLUSTER_NAME-config.yaml"
    printf '%s\n' \
        'kind: Cluster' \
        'apiVersion: kind.x-k8s.io/v1alpha4' \
        'networking:' \
        '  apiServerAddress: "127.0.0.1"' \
        'featureGates:' \
        '  ClusterTrustBundle: true' > "$config"
    run kind create cluster --name "$CLUSTER_NAME" --image "$KIND_NODE_IMAGE" --config "$config" --kubeconfig "$KUBECONFIG" --wait "$WAIT_TIMEOUT"
    check_cluster
    uid=$(kube get namespace kube-system -o 'jsonpath={.metadata.uid}')
    server=$(kube config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
    [[ -n "$uid" && -n "$server" ]] || fail 'Created cluster identity could not be verified; no ownership marker written.'
    run jq -n --arg name "$CLUSTER_NAME" --arg context "$KUBE_CONTEXT" --arg uid "$uid" \
        --arg server "$server" --arg root "$ROOT" --arg kubeconfig "$KUBECONFIG" \
        '{name:$name,context:$context,uid:$uid,server:$server,root:$root,kubeconfig:$kubeconfig,created:true}' > "$marker"
    printf 'Created owned kind cluster %s.\n' "$CLUSTER_NAME"
}

wait_credentials() {
    local secret="$1" attempt payload
    for ((attempt=1; attempt<=READINESS_ATTEMPTS; attempt++)); do
        if payload=$(kube get secret "$secret" --namespace "$NAMESPACE" -o json 2>/dev/null) && \
            run jq -e '.data.username != null and .data.password != null and (.data.username | length) > 0 and (.data.password | length) > 0' > /dev/null <<< "$payload"; then
            return
        fi
        sleep "$READINESS_INTERVAL"
    done
    fail "Credentials did not become available: $secret."
}

wait_sql() {
    local secret="$1" database="$2" db_service="$3" db_port="$4" attempt credentials username password pod output
    wait_credentials "$secret"
    credentials=$(kube get secret "$secret" --namespace "$NAMESPACE" -o json)
    username=$(run jq -er '.data.username | @base64d' <<< "$credentials")
    password=$(run jq -er '.data.password | @base64d' <<< "$credentials")
    for ((attempt=1; attempt<=READINESS_ATTEMPTS; attempt++)); do
        pod=$(kube get pods --namespace "$NAMESPACE" \
            --selector "cluster-name=$db_service,spilo-role=master" -o json | \
            run jq -r '[.items[] | select(.status.phase == "Running") | .metadata.name][0] // empty')
        if [[ -n "$pod" ]] && output=$(printf '%s\n' "$password" | \
            bounded_sql_exec exec --namespace "$NAMESPACE" -i "$pod" -- bash -c \
            'IFS= read -r PGPASSWORD; export PGPASSWORD PGSSLMODE=require PGCONNECT_TIMEOUT=5; export PGOPTIONS="-c statement_timeout=5000 -c lock_timeout=5000"; exec psql --host "$1" --port "$2" --username "$3" --dbname "$4" --no-psqlrc --tuples-only --no-align --command "SELECT 1"' \
            readiness "$db_service" "$db_port" "$username" "$database" 2>/dev/null) && [[ "$output" == 1 ]]; then
            printf 'Authenticated SQL ready for database %s.\n' "$database"
            return
        fi
        sleep "$READINESS_INTERVAL"
    done
    fail "Authenticated SQL readiness failed for $database (credential secret $secret)."
}

wait_configmap_key() {
    local name="$1" key="$2" attempt payload
    for ((attempt=1; attempt<=READINESS_ATTEMPTS; attempt++)); do
        if payload=$(kube get configmap "$name" --namespace "$NAMESPACE" -o json 2>/dev/null) && \
            run jq -e --arg key "$key" '.data[$key] | type == "string" and length > 0' > /dev/null <<< "$payload"; then
            return
        fi
        sleep "$READINESS_INTERVAL"
    done
    fail "CA prerequisite did not become ready: ConfigMap $name key $key."
}

copy_resource() {
    local kind="$1" name="$2"
    kube get "$kind" "$name" --namespace "$NAMESPACE" -o json | \
        run jq --arg namespace "$SUBSTRATE_NAMESPACE" \
        'del(.metadata.uid,.metadata.resourceVersion,.metadata.creationTimestamp,.metadata.managedFields,.metadata.ownerReferences) | .metadata.namespace=$namespace | del(.metadata.annotations."meta.helm.sh/release-name",.metadata.annotations."meta.helm.sh/release-namespace")' | \
        kube apply --namespace "$SUBSTRATE_NAMESPACE" -f -
}

wait_deployments() {
    local namespace="$1" release="$2" names name
    names=$(kube get deployments --namespace "$namespace" \
        --selector "app.kubernetes.io/instance=$release" -o json | run jq -r '.items[].metadata.name')
    [[ -n "$names" ]] || fail "No deployments discovered for release $release in $namespace."
    while IFS= read -r name; do
        kube rollout status "deployment/$name" --namespace "$namespace" --timeout "$WAIT_TIMEOUT"
    done <<< "$names"
}

build_substrate_options() {
    substrate_args=(--namespace "$SUBSTRATE_NAMESPACE")
    if [[ -n "$CLUSTER_NAME" ]]; then
        substrate_args+=(--set ateapi.image.pullPolicy=IfNotPresent --set atecontroller.image.pullPolicy=IfNotPresent)
    fi
    if [[ -n "${SUBSTRATE_VALUES:-}" ]]; then substrate_args+=(-f "$SUBSTRATE_VALUES"); fi
    SUBSTRATE_API_PORT=$(run helm template "$SUBSTRATE_RELEASE_NAME" charts/agent-substrate "${substrate_args[@]}" \
        --show-only templates/deployment-ateapi.yaml | run uv run --locked python -c '
import sys, yaml
services = [doc for doc in yaml.safe_load_all(sys.stdin) if doc and doc.get("kind") == "Service" and doc.get("spec", {}).get("selector", {}).get("app.kubernetes.io/component") == "ateapi"]
if len(services) != 1:
    raise ValueError("Expected exactly one substrate API Service")
ports = [entry["port"] for entry in services[0]["spec"]["ports"] if entry.get("name") == "grpc"]
if len(ports) != 1 or type(ports[0]) is not int or not 1 <= ports[0] <= 65535:
    raise ValueError("Expected one valid substrate API gRPC port")
print(ports[0])
')
    substrate_args+=(--set "ateapi.service.port=$SUBSTRATE_API_PORT")
}

build_platform_options() {
    platform_args=(--namespace "$NAMESPACE" --set "workers.image.tag=$IMAGE_TAG" \
        --set "unifiedApi.image.tag=$IMAGE_TAG" --set "frontend.image.tag=$IMAGE_TAG" \
        --set "caddyTailscale.image.tag=$IMAGE_TAG" \
        --set "workers.substrateNamespace=$SUBSTRATE_NAMESPACE" --set "rustfs.substrateNamespace=$SUBSTRATE_NAMESPACE")
    if [[ -n "$CLUSTER_NAME" || "$PROFILE" == kind ]]; then
        platform_args+=(-f charts/platform/values-kind.yaml)
    fi
    if [[ -n "$CLUSTER_NAME" ]]; then
        platform_args+=(--set unifiedApi.image.pullPolicy=IfNotPresent \
            --set frontend.image.pullPolicy=IfNotPresent \
            --set workers.image.pullPolicy=IfNotPresent \
            --set caddyTailscale.image.pullPolicy=IfNotPresent)
    fi
    if [[ -n "${DEPLOY_VALUES:-}" ]]; then platform_args+=(-f "$DEPLOY_VALUES"); fi
    mkdir -p "$ROOT/.cache/deploy"
    PRIVATE_VALUES_FILE=$(umask 077; mktemp "$ROOT/.cache/deploy/private-values.XXXXXXXX")
    run jq '{secrets:({langfuseNextauthSecret:.LANGFUSE_NEXTAUTH_SECRET,langfuseSalt:.LANGFUSE_SALT}|with_entries(select(.value|length>0)))}' \
        <<< "$APPLICATION_SETTINGS" > "$PRIVATE_VALUES_FILE"
    platform_args+=(-f "$PRIVATE_VALUES_FILE")
    local tailnet
    tailnet=$(run jq -r '.TAILSCALE_DOMAIN' <<< "$APPLICATION_SETTINGS")
    if [[ -n "$tailnet" ]]; then platform_args+=(--set-string "caddyTailscale.tailnet=$tailnet"); fi
    platform_args+=(${HELM_OPTIONS[@]+"${HELM_OPTIONS[@]}"})
    platform_args+=(--set "workers.substratePort=$SUBSTRATE_API_PORT")
    if [[ "$PROFILE" == kind ]]; then
        platform_args+=(--set temporal.enabled=false --set temporalDev.enabled=true --set workers.enabled=true)
    else
        platform_args+=(--set temporal.enabled=true --set temporalDev.enabled=false --set workers.enabled=true)
    fi
}

resolve_platform_contract() {
    # Render locally to resolve chart-derived resource names/ports without a cluster ConfigMap.
    CONTRACT=$(run helm template "$RELEASE_NAME" charts/platform "${platform_args[@]}" | run uv run --locked python -c '
import json, sys, yaml
docs = [doc for doc in yaml.safe_load_all(sys.stdin) if isinstance(doc, dict)]
def by_kind(kind):
    return [d for d in docs if d.get("kind") == kind]
pg = by_kind("postgresql")[0]
db_name = pg["metadata"]["name"]
db_ns = sys.argv[1]
db_dns = f"{db_name}.{db_ns}.svc.cluster.local"
db_users = list(pg["spec"]["users"].keys())
substrate_user = [u for u in db_users if u not in {"platform", "langfuse", "temporal"}][0]
substrate_db = [k for k, v in pg["spec"]["databases"].items() if v == substrate_user][0]
rustfs_svc = [s for s in by_kind("Service") if s.get("spec", {}).get("selector", {}).get("app") == "rustfs"][0]
rustfs_secret = [s for s in by_kind("Secret") if s["metadata"]["name"].endswith("-rustfs-auth")][0]["metadata"]["name"]
api_svc = [s for s in by_kind("Service") if s.get("spec", {}).get("selector", {}).get("app") == "unified-api"][0]
workers_dep = [d for d in by_kind("Deployment") if d.get("spec", {}).get("selector", {}).get("matchLabels", {}).get("app") == "workers"][0]
worker_env = {e["name"]: e.get("value", "") for e in workers_dep["spec"]["template"]["spec"]["containers"][0]["env"]}
temporal_url = worker_env["TEMPORAL_URL"]
temporal_svc = temporal_url.split(".")[0]
temporal_port = temporal_url.rsplit(":", 1)[1]
trust_cm = [c for c in by_kind("ConfigMap") if c["metadata"]["name"] == f"{db_name}-trust"][0]
trust_key = list(trust_cm.get("data", {}).keys())[0] if trust_cm.get("data") else "ca.crt"
substrate_secret_user = substrate_user.replace("_", "-")
json.dump({
    "databaseResource": db_name,
    "databaseService": db_name,
    "databaseDNS": db_dns,
    "databasePort": "5432",
    "databaseUser": "platform",
    "databaseName": "platform",
    "databaseCredentialSecret": f"platform.{db_name}.credentials.postgresql.acid.zalan.do",
    "temporalCredentialSecret": f"temporal.{db_name}.credentials.postgresql.acid.zalan.do",
    "temporalDatabase": "temporal",
    "temporalVisibilityDatabase": "temporal_visibility",
    "substrateDatabaseUser": substrate_user,
    "substrateDatabaseName": substrate_db,
    "substrateDatabaseCredentialSecret": f"{substrate_secret_user}.{db_name}.credentials.postgresql.acid.zalan.do",
    "databaseTrustConfigMap": trust_cm["metadata"]["name"],
    "databaseTrustKey": trust_key,
    "rustfsService": rustfs_svc["metadata"]["name"],
    "rustfsPort": str(rustfs_svc["spec"]["ports"][0]["port"]),
    "rustfsSecret": rustfs_secret,
    "apiService": api_svc["metadata"]["name"],
    "apiPort": str(api_svc["spec"]["ports"][0]["port"]),
    "temporalService": temporal_svc,
    "temporalPort": temporal_port,
    "temporalNamespace": worker_env["TEMPORAL_NAMESPACE"],
    "temporalTaskQueue": worker_env["TEMPORAL_TASK_QUEUE"],
}, sys.stdout)
' "$NAMESPACE")
}

contract_value() {
    run jq -er --arg key "$1" '.[$key] | select(type == "string" and length > 0)' <<< "$CONTRACT"
}

deploy() {
    validate_helm_options
    check_cluster
    load_application_settings
    local platform_args=() substrate_args=()
    build_substrate_options
    build_platform_options
    resolve_platform_contract

    helm_context upgrade --install "$PROVIDER_RELEASE_NAME" postgres-operator \
        --repo https://opensource.zalando.com/postgres-operator/charts/postgres-operator \
        --version "$POSTGRES_OPERATOR_VERSION" \
        --namespace "$PROVIDER_NAMESPACE" --create-namespace \
        -f charts/postgres-operator-values.yaml \
        --wait --timeout "$WAIT_TIMEOUT"
    wait_deployments "$PROVIDER_NAMESPACE" "$PROVIDER_RELEASE_NAME"
    kube wait --for=condition=Established crd/postgresqls.acid.zalan.do --timeout "$WAIT_TIMEOUT"

    kube create namespace "$NAMESPACE" --dry-run=client -o json | kube apply -f -
    if run jq -e '.GH_PAT | length > 0' > /dev/null <<< "$APPLICATION_SETTINGS"; then
        run jq --arg namespace "$NAMESPACE" \
            'if (.GH_USER | length) == 0 then error("GH_USER is required with GH_PAT") else {apiVersion:"v1",kind:"Secret",metadata:{name:"ghcr-secret",namespace:$namespace},type:"kubernetes.io/dockerconfigjson",data:{".dockerconfigjson":({auths:{"ghcr.io":{username:.GH_USER,password:.GH_PAT,auth:((.GH_USER+":"+.GH_PAT)|@base64)}}}|tojson|@base64)}} end' <<< "$APPLICATION_SETTINGS" | kube apply -f - # betterleaks:allow
    fi
    if run jq -e '.TAILSCALE_AUTH_KEY | length > 0' > /dev/null <<< "$APPLICATION_SETTINGS"; then
        run jq --arg namespace "$NAMESPACE" \
            '{apiVersion:"v1",kind:"Secret",metadata:{name:"tailscale-auth",namespace:$namespace},type:"Opaque",data:{TS_AUTHKEY:(.TAILSCALE_AUTH_KEY|@base64)}}' \
            <<< "$APPLICATION_SETTINGS" | kube apply -f -
    fi

    helm_context upgrade --install "$RELEASE_NAME" charts/platform "${platform_args[@]}" \
        --wait --timeout "$WAIT_TIMEOUT"

    local db_service db_port db_cred_secret db_name sub_cred_secret sub_db_name temp_cred_secret temp_db_name temp_vis_db trust trust_key rustfs_secret
    IFS=$'\t' read -r db_service db_port db_cred_secret db_name sub_cred_secret sub_db_name temp_cred_secret temp_db_name temp_vis_db trust trust_key rustfs_secret < <(
        run jq -er '[.databaseService, .databasePort, .databaseCredentialSecret, .databaseName, .substrateDatabaseCredentialSecret, .substrateDatabaseName, .temporalCredentialSecret, .temporalDatabase, .temporalVisibilityDatabase, .databaseTrustConfigMap, .databaseTrustKey, .rustfsSecret] | if all(.[]; type == "string" and length > 0) then @tsv else error("missing contract field") end' <<< "$CONTRACT"
    )
    kube wait --for=create "postgresql/$db_service" --namespace "$NAMESPACE" --timeout "$WAIT_TIMEOUT"
    wait_sql "$db_cred_secret" "$db_name" "$db_service" "$db_port"
    wait_sql "$sub_cred_secret" "$sub_db_name" "$db_service" "$db_port"
    if [[ "$PROFILE" == full ]]; then
        wait_sql "$temp_cred_secret" "$temp_db_name" "$db_service" "$db_port"
        wait_sql "$temp_cred_secret" "$temp_vis_db" "$db_service" "$db_port"
    fi
    wait_configmap_key "$trust" "$trust_key"
    wait_configmap_key kube-root-ca.crt ca.crt
    wait_deployments "$NAMESPACE" "$RELEASE_NAME"

    kube create namespace "$SUBSTRATE_NAMESPACE" --dry-run=client -o json | kube apply -f -
    copy_resource secret "$rustfs_secret"
    copy_resource secret "$sub_cred_secret"
    copy_resource configmap "$trust"
    SUBSTRATE_VALUES_FILE=$(umask 077; mktemp "$ROOT/.cache/deploy/substrate-values.XXXXXXXX")
    run jq -n --argjson c "$CONTRACT" --arg ns "$NAMESPACE" --arg tag "$IMAGE_TAG" \
        '{platformNamespace:$ns,global:{image:{tag:$tag}},database:{host:$c.databaseService,port:($c.databasePort|tonumber),user:$c.substrateDatabaseUser,name:$c.substrateDatabaseName,credentialSecret:$c.substrateDatabaseCredentialSecret,trustConfigMap:$c.databaseTrustConfigMap,trustKey:$c.databaseTrustKey},rustfs:{host:$c.rustfsService,port:($c.rustfsPort|tonumber),secretName:$c.rustfsSecret}}' > "$SUBSTRATE_VALUES_FILE"
    helm_context upgrade --install "$SUBSTRATE_RELEASE_NAME" charts/agent-substrate \
        -f "$SUBSTRATE_VALUES_FILE" "${substrate_args[@]}" \
        --wait --timeout "$WAIT_TIMEOUT"
    wait_deployments "$SUBSTRATE_NAMESPACE" "$SUBSTRATE_RELEASE_NAME"
    printf 'Deployed %s profile to context %s; namespace=%s release=%s.\n' "$PROFILE" "$KUBE_CONTEXT" "$NAMESPACE" "$RELEASE_NAME"
}

COMMAND=${1:-}
if [[ "$COMMAND" == --help || "$COMMAND" == -h ]]; then usage; exit 0; fi
[[ -n "$COMMAND" ]] || { usage; exit 1; }
shift
if [[ "${1:-}" == -- ]]; then shift; fi
HELM_OPTIONS=("$@")
case "$COMMAND" in
    cluster-up) cluster_up ;;
    check-cluster) check_cluster ;;
    deploy) deploy ;;
    *) usage; fail "Unknown lifecycle command: $COMMAND" ;;
esac
