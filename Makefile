SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

MISE := mise
RUN := $(MISE) exec --
NX := $(RUN) pnpm --dir apps/ui exec nx
# renovate: datasource=pypi depName=checkov
CHECKOV_VERSION ?= 3.3.26
KUBECONFORM_FLAGS := -strict -summary -skip KataConfig -schema-location default -schema-location '.schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
CLUSTER_NAME ?= platform-dev
KUBE_CONTEXT ?= $(if $(CLUSTER_NAME),kind-$(CLUSTER_NAME),)
KUBECONFIG := $(if $(CLUSTER_NAME),$(CURDIR)/.cache/clusters/$(CLUSTER_NAME)/kubeconfig,$(KUBECONFIG))
NAMESPACE ?= platform
RELEASE_NAME ?= platform
SUBSTRATE_NAMESPACE ?= agent-substrate
SUBSTRATE_RELEASE_NAME ?= agent-substrate
PROVIDER_NAMESPACE ?= $(NAMESPACE)
PROVIDER_RELEASE_NAME ?= postgres-operator
PROFILE ?= kind
IMAGE_TAG ?= local
CADDY_IMAGE ?= ghcr.io/agent-director/caddy-tailscale:$(IMAGE_TAG)
PYTEST_ARGS ?=

export CLUSTER_NAME KUBE_CONTEXT KUBECONFIG NAMESPACE RELEASE_NAME SUBSTRATE_NAMESPACE
export SUBSTRATE_RELEASE_NAME PROVIDER_NAMESPACE PROVIDER_RELEASE_NAME PROFILE IMAGE_TAG CADDY_IMAGE

.PHONY: help setup typecheck build-frontend lint format test pytest-unit image-matrix nx-graph nx-affected graphify
.PHONY: cluster-up cluster-down check-cluster deploy teardown test-integration pytest-integration
.PHONY: lint-helm lint-checkov lint-kubernetes lint-kube check-helm-deps update-schemas check-schema
.PHONY: scan-iac scan-secrets scan-secrets-staged scan-image scan-local actions-lock verify-actions-lock

help:
	@printf '%s\n' 'setup / typecheck / build-frontend / lint / format / pytest-unit' \
	  'cluster-up [CLUSTER_NAME=platform-dev] [REUSE_CLUSTER=true]' \
	  'deploy [CLUSTER_NAME=platform-dev] [PROFILE=kind|full] [DEPLOY_VALUES=<yaml>]' \
	  'test-integration [CLUSTER_NAME=platform-dev] (uses an existing cluster)' \
	  'teardown [CLUSTER_NAME=platform-dev] (owned local kind cluster only)'

setup:
	$(MISE) install --locked
	$(RUN) uv sync --locked --all-packages
	$(RUN) pnpm --dir apps/ui install --frozen-lockfile
	$(MAKE) check-helm-deps
	@if [[ "$${CI:-false}" != "true" ]]; then $(RUN) prek install; fi

typecheck:
	$(RUN) uv run --locked --no-sync pyrefly check --config pyproject.toml
	$(RUN) pnpm --dir apps/ui typecheck

build-frontend:
	$(RUN) pnpm --dir apps/ui build

lint:
	$(RUN) prek run --all-files

format:
	$(RUN) prek run ruff-format --all-files

test: pytest-unit

pytest-unit:
	$(RUN) uv run --locked --no-sync pytest $(PYTEST_ARGS)

image-matrix:
	@$(RUN) uv run --no-project tools/scripts/images.py --format matrix

nx-graph:
	$(NX) graph --file=.cache/nx/graph.json

nx-affected:
	$(NX) affected -t lint,typecheck,test,build --parallel=3

graphify:
	GRAPHIFY_BACKEND=none $(RUN) uvx --from "graphifyy==0.9.92" graphify extract . --code-only && GRAPHIFY_BACKEND=none $(RUN) uvx --from "graphifyy==0.9.92" graphify cluster-only . --no-viz --no-label

cluster-up:
	./tools/scripts/deploy.sh cluster-up

cluster-down: teardown

check-cluster:
	./tools/scripts/deploy.sh check-cluster

deploy:
	./tools/scripts/deploy.sh deploy

teardown:
	./tools/scripts/teardown.sh "$(CLUSTER_NAME)"

test-integration:
	./tools/scripts/test-integration.sh "$(CLUSTER_NAME)" $(PYTEST_ARGS)

pytest-integration: check-cluster
	$(RUN) uv run --locked pytest tests/integration/ -n 0 $(PYTEST_ARGS)

lint-helm: check-helm-deps
	@for chart in charts/*/Chart.yaml; do \
	  $(RUN) helm lint "$${chart%/Chart.yaml}"; \
	done
	@$(RUN) helm template platform charts/platform --namespace platform > /dev/null
	@$(RUN) helm template platform charts/platform --namespace platform -f charts/platform/values-kind.yaml > /dev/null
	@$(RUN) helm template agent-substrate charts/agent-substrate --namespace agent-substrate > /dev/null

check-helm-deps:
	@if [[ ! -f charts/platform/charts/temporal-1.7.0.tgz ]]; then \
	  $(RUN) helm repo add temporal https://go.temporal.io/helm-charts --force-update; \
	  $(RUN) helm dependency build charts/platform --skip-refresh; \
	fi

update-schemas:
	$(RUN) uv run --locked tools/scripts/update-crds.py all

lint-checkov:
	env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
	  $(RUN) uvx "checkov@$(CHECKOV_VERSION)" --framework helm -d charts --config-file .checkov.yaml --quiet --compact

lint-kube: lint-kubernetes

lint-kubernetes: check-helm-deps
	$(RUN) helm template platform charts/platform --namespace platform | $(RUN) kube-linter lint - --config .kube-linter.yaml
	$(RUN) helm template platform charts/platform --namespace platform -f charts/platform/values-kind.yaml | $(RUN) kube-linter lint - --config .kube-linter.yaml
	$(RUN) helm template agent-substrate charts/agent-substrate --namespace agent-substrate | $(RUN) kube-linter lint - --config .kube-linter.yaml

check-schema: check-helm-deps
	$(RUN) helm template platform charts/platform --namespace platform | $(RUN) kubeconform $(KUBECONFORM_FLAGS)
	$(RUN) helm template platform charts/platform --namespace platform -f charts/platform/values-kind.yaml | $(RUN) kubeconform $(KUBECONFORM_FLAGS)
	$(RUN) helm template agent-substrate charts/agent-substrate --namespace agent-substrate | $(RUN) kubeconform $(KUBECONFORM_FLAGS)

scan-iac:
	$(RUN) trivy fs --config trivy.yaml --scanners misconfig,secret .

scan-secrets:
	$(RUN) gitleaks git --log-opts="HEAD" --redact --config .gitleaks.toml

scan-secrets-staged:
	$(RUN) gitleaks git --pre-commit --staged --redact --config .gitleaks.toml

TRIVY_IMAGE_FLAGS ?=

scan-image:
	@[[ -n "$${IMAGE_REF:-}" ]] || { printf 'ERROR: Set IMAGE_REF=<image:tag>\n' >&2; exit 1; }
	@mkdir -p .cache/trivy/reports; \
	full_report="$$(mktemp .cache/trivy/reports/full.XXXXXXXX.json)"; \
	lib_report="$$(mktemp .cache/trivy/reports/lib.XXXXXXXX.json)"; \
	os_report="$$(mktemp .cache/trivy/reports/os.XXXXXXXX.json)"; \
	trap 'rm -f "$$full_report" "$$lib_report" "$$os_report"' EXIT; \
	$(RUN) trivy image --config trivy.yaml $(TRIVY_IMAGE_FLAGS) --scanners vuln,secret \
	  --pkg-types os,library --severity HIGH,CRITICAL --exit-code 0 \
	  --format json --output "$$full_report" "$$IMAGE_REF"; \
	$(RUN) jq 'if .Results then .Results |= map(select(.Class != "os-pkgs")) else . end' "$$full_report" > "$$lib_report"; \
	$(RUN) jq 'if .Results then .Results |= map(select(.Class == "os-pkgs")) else . end' "$$full_report" > "$$os_report"; \
	$(RUN) trivy convert --config trivy.yaml --scanners vuln,secret \
	  --format table --severity HIGH,CRITICAL --exit-code 1 "$$lib_report"; \
	$(RUN) trivy convert --config trivy.yaml --scanners vuln \
	  --format table --severity HIGH --exit-code 0 "$$os_report"; \
	$(RUN) trivy convert --config trivy.yaml --scanners vuln \
	  --format table --severity CRITICAL --exit-code 1 "$$os_report"

scan-local:
	@selection="$(if $(IMAGE),$(IMAGE),affected)"; \
	diff_files=""; \
	if [[ "$$selection" == "affected" ]]; then \
	  base_ref="$$(git rev-parse --verify -q @{upstream} 2>/dev/null || git rev-parse --verify -q upstream/main 2>/dev/null || git rev-parse --verify -q origin/main 2>/dev/null || true)"; \
	  if [[ -n "$$base_ref" ]]; then \
	    diff_files="$$(git diff --name-only "$$base_ref"...HEAD 2>/dev/null || true)"; \
	    if [[ -z "$$diff_files" ]]; then \
	      diff_files="$$(git status --porcelain | awk '{print $$NF}')"; \
	    fi; \
	  else \
	    selection="all"; \
	  fi; \
	fi; \
	image_Affected() { \
	  local name="$$1" dockerfile="$$2" component="$${2#images/}"; component="$${component%/Dockerfile}"; \
	  [[ -n "$$diff_files" ]] || return 1; \
	  if grep -qE '^(mise\.toml|mise\.lock|trivy\.yaml|\.trivyignore\.yaml|\.dockerignore)$$' <<< "$$diff_files"; then return 0; fi; \
	  if grep -qE "^images/$$component/" <<< "$$diff_files"; then return 0; fi; \
	  case "$$name" in \
	    unified-api) grep -qE '^(pyproject\.toml|uv\.lock|apps/core/pyproject\.toml|apps/core/unified_api/|libs/contracts/)' <<< "$$diff_files" && return 0 ;; \
	    platform-worker) grep -qE '^(pyproject\.toml|uv\.lock|apps/core/pyproject\.toml|apps/core/orchestrator/|libs/contracts/)' <<< "$$diff_files" && return 0 ;; \
	    sandbox) grep -qE '^(pyproject\.toml|uv\.lock|apps/sandbox-exec/|libs/contracts/)' <<< "$$diff_files" && return 0 ;; \
	    frontend) grep -qE '^apps/ui/' <<< "$$diff_files" && return 0 ;; \
	  esac; \
	  return 1; \
	}; \
	targets=(); \
	matched=0; \
	while IFS=$$'\t' read -r name dockerfile target; do \
	  [[ -n "$$name" ]] || continue; \
	  component="$${dockerfile#images/}"; component="$${component%/Dockerfile}"; \
	  if [[ "$$selection" == "all" || "$$selection" == "$$name" || "$$selection" == "$$component" || "$$selection" == "images/$$component" ]]; then \
	    matched=1; \
	    targets+=("$$name|$$dockerfile|$$target"); \
	  elif [[ "$$selection" == "affected" ]]; then \
	    matched=1; \
	    if image_Affected "$$name" "$$dockerfile"; then \
	      targets+=("$$name|$$dockerfile|$$target"); \
	    fi; \
	  fi; \
	done < <($(RUN) uv run --no-project tools/scripts/images.py --format tsv); \
	[[ "$$matched" -eq 1 ]] || { printf 'ERROR: Unknown image selection: %s\n' "$$selection" >&2; exit 2; }; \
	if [[ "$${#targets[@]}" -eq 0 ]]; then \
	  printf 'No image or runtime dependency files modified; skipping local container scans.\n'; \
	  exit 0; \
	fi; \
	if [[ ! -f .cache/trivy/db/trivy.db ]]; then \
	  $(RUN) trivy image --config trivy.yaml --download-db-only; \
	fi; \
	mkdir -p .cache/trivy/local-scans .cache/trivy/workers; \
	rm -f .cache/trivy/local-scans/*.log .cache/trivy/local-scans/*.status; \
	pids=(); \
	for entry in "$${targets[@]}"; do \
	  IFS='|' read -r name dockerfile target <<< "$$entry"; \
	  ( \
	    set -euo pipefail; \
	    tag="local-scan/$$name:latest"; \
	    target_args=(); if [[ -n "$$target" ]]; then target_args=(--target "$$target"); fi; \
	    worker_cache=".cache/trivy/workers/$$name"; \
	    mkdir -p "$$worker_cache"; \
	    ln -sfn "$(CURDIR)/.cache/trivy/db" "$$worker_cache/db"; \
	    if [[ -d "$(CURDIR)/.cache/trivy/java-db" ]]; then ln -sfn "$(CURDIR)/.cache/trivy/java-db" "$$worker_cache/java-db"; fi; \
	    $(RUN) docker buildx build -f "$$dockerfile" --load $${target_args[@]+"$${target_args[@]}"} -t "$$tag" .; \
	    $(MAKE) scan-image IMAGE_REF="$$tag" TRIVY_IMAGE_FLAGS="--cache-dir $$worker_cache --skip-db-update --skip-java-db-update"; \
	  ) > ".cache/trivy/local-scans/$$name.log" 2>&1 & \
	  pids+=("$$!:$$name"); \
	  if [[ "$${CI:-false}" == "true" ]]; then wait "$${pids[-1]%%:*}" || true; fi; \
	done; \
	failures=0; \
	for item in "$${pids[@]}"; do \
	  pid="$${item%%:*}"; name="$${item#*:}"; \
	  if wait "$$pid"; then \
	    printf 'PASS %-28s\n' "$$name"; \
	  else \
	    printf 'FAIL %-28s\n' "$$name" >&2; \
	    cat ".cache/trivy/local-scans/$$name.log" >&2; \
	    failures=$$((failures + 1)); \
	  fi; \
	done; \
	[[ "$$failures" -eq 0 ]] || exit 1

actions-lock:
	gh actions-lock --no-migrate-local-actions

verify-actions-lock:
	gh actions-lock --verify --no-migrate-local-actions
