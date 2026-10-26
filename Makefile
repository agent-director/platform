SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

MISE := mise
RUN := $(MISE) exec --
NX := $(RUN) pnpm exec nx
NX_PARALLEL ?= 3
NX_CI_TARGETS := lint,format-check,typecheck,test,build
NX_PYTEST_PROJECTS := core,sandbox-exec,contracts,repo-tests
# Prek hooks whose checks are owned by cached Nx targets; `lint-residual` skips them in CI.
# Keep in sync with project.json targets (tests/unit/test_security_gates.py enforces parity).
NX_OWNED_HOOKS := ruff ruff-format biome typecheck pytest helm-lint kubeconform kube-linter checkov
GRAPHIFY := $(RUN) uv run --locked --only-group graphify graphify
GRAPHIFY_BACKEND ?= none
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

.PHONY: help setup typecheck build-frontend lint lint-residual format test pytest-unit image-matrix nx-graph nx-affected nx-ci ci graphify graphify-fetch dev-temporal
.PHONY: cluster-up cluster-down check-cluster deploy teardown test-integration pytest-integration
.PHONY: lint-helm lint-checkov lint-kubernetes lint-kube update-schemas check-schema
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
	$(RUN) pnpm install --frozen-lockfile
	@if [[ "$${CI:-false}" != "true" ]]; then $(RUN) prek install; fi

typecheck:
	$(NX) run-many -t typecheck --parallel=$(NX_PARALLEL)

build-frontend:
	$(NX) run ui:build

lint:
	$(RUN) prek run --all-files

# Commit gates Nx does not own (whitespace/EOF/syntax, actions-lock, betterleaks, actionlint, hadolint).
lint-residual:
	$(RUN) prek run --all-files $(addprefix --skip ,$(NX_OWNED_HOOKS))

format:
	$(RUN) prek run ruff-format --all-files

test:
	$(NX) run-many -t test --parallel=$(NX_PARALLEL)

# Cached through Nx unless PYTEST_ARGS selects an ad-hoc run.
pytest-unit:
ifeq ($(strip $(PYTEST_ARGS)),)
	$(NX) run-many -t test -p $(NX_PYTEST_PROJECTS) --parallel=$(NX_PARALLEL)
else
	$(RUN) uv run --locked --no-sync pytest $(PYTEST_ARGS)
endif

image-matrix:
	@$(RUN) uv run --no-project tools/scripts/images.py --format matrix

nx-graph:
	$(NX) graph --print

nx-affected:
	$(NX) affected -t $(NX_CI_TARGETS) --parallel=$(NX_PARALLEL)

# Every Nx-owned gate, exact-input cached (the CI path).
nx-ci:
	$(NX) run-many -t $(NX_CI_TARGETS) --parallel=$(NX_PARALLEL)

ci: nx-ci lint-residual

graphify:
	@if [[ "$(GRAPHIFY_BACKEND)" != "none" ]]; then \
	  GRAPHIFY_BACKEND="$(GRAPHIFY_BACKEND)" $(GRAPHIFY) extract . --backend "$(GRAPHIFY_BACKEND)" && \
	  GRAPHIFY_BACKEND="$(GRAPHIFY_BACKEND)" $(GRAPHIFY) cluster-only . --no-viz --backend "$(GRAPHIFY_BACKEND)"; \
	else \
	  if [[ -f graphify-out/graph.json && -f graphify-out/manifest.json ]]; then \
	    GRAPHIFY_BACKEND=none $(GRAPHIFY) update .; \
	  else \
	    GRAPHIFY_BACKEND=none $(GRAPHIFY) extract . --code-only; \
	  fi && \
	  GRAPHIFY_BACKEND=none $(GRAPHIFY) cluster-only . --no-viz --no-label; \
	fi

graphify-fetch:
	./tools/scripts/fetch-graphify-brain.sh

dev-temporal:
	@mkdir -p .cache/temporal
	$(RUN) temporal server start-dev --ip 127.0.0.1 --port 7233 --ui-port 8233 --db-filename .cache/temporal/dev.db

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

lint-helm:
	@for chart in charts/*/Chart.yaml; do \
	  $(RUN) helm lint "$${chart%/Chart.yaml}"; \
	done
	@$(RUN) helm template platform charts/platform --namespace platform > /dev/null
	@$(RUN) helm template platform charts/platform --namespace platform -f charts/platform/values-kind.yaml > /dev/null
	@$(RUN) helm template agent-substrate charts/agent-substrate --namespace agent-substrate > /dev/null

update-schemas:
	$(RUN) uv run --locked tools/scripts/update-crds.py all

lint-checkov:
	env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
	  $(RUN) uvx "checkov@$(CHECKOV_VERSION)" --framework helm -d charts --config-file .checkov.yaml --quiet --compact
	env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
	  $(RUN) uvx "checkov@$(CHECKOV_VERSION)" --framework helm -d charts/platform --var-file charts/platform/values-kind.yaml --config-file .checkov.yaml --quiet --compact

lint-kube: lint-kubernetes

lint-kubernetes:
	$(RUN) helm template platform charts/platform --namespace platform | $(RUN) kube-linter lint - --config .kube-linter.yaml
	$(RUN) helm template platform charts/platform --namespace platform -f charts/platform/values-kind.yaml | $(RUN) kube-linter lint - --config .kube-linter.yaml
	$(RUN) helm template agent-substrate charts/agent-substrate --namespace agent-substrate | $(RUN) kube-linter lint - --config .kube-linter.yaml

check-schema:
	$(RUN) helm template platform charts/platform --namespace platform | $(RUN) kubeconform $(KUBECONFORM_FLAGS)
	$(RUN) helm template platform charts/platform --namespace platform -f charts/platform/values-kind.yaml | $(RUN) kubeconform $(KUBECONFORM_FLAGS)
	$(RUN) helm template agent-substrate charts/agent-substrate --namespace agent-substrate | $(RUN) kubeconform $(KUBECONFORM_FLAGS)

scan-iac:
	$(RUN) trivy fs --config trivy.yaml --scanners misconfig,secret .

scan-secrets:
	$(RUN) betterleaks git . --log-opts="HEAD" --redact --config .betterleaks.toml

scan-secrets-staged:
	$(RUN) betterleaks git . --pre-commit --staged --redact --config .betterleaks.toml

TRIVY_IMAGE_FLAGS ?=

scan-image:
	MISE="$(MISE)" TRIVY_IMAGE_FLAGS="$(TRIVY_IMAGE_FLAGS)" ./tools/scripts/scan-images.sh scan-image "$(IMAGE_REF)"

scan-local:
	MISE="$(MISE)" ./tools/scripts/scan-images.sh scan-local "$(IMAGE)"

actions-lock:
	gh actions-lock --no-migrate-local-actions

verify-actions-lock:
	gh actions-lock --verify --no-migrate-local-actions
