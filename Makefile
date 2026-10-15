SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

MISE := mise
RUN := $(MISE) exec --
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

.PHONY: help setup typecheck build-frontend lint format test pytest-unit image-matrix
.PHONY: cluster-up cluster-down check-cluster deploy teardown test-integration pytest-integration
.PHONY: lint-helm lint-checkov lint-kubernetes lint-kube check-helm-deps update-schemas check-schema
.PHONY: scan-iac scan-secrets scan-image scan-local verify-actions-lock

help:
	@printf '%s\n' 'setup / typecheck / build-frontend / lint / format / pytest-unit' \
	  'cluster-up [CLUSTER_NAME=platform-dev] [REUSE_CLUSTER=true]' \
	  'deploy [CLUSTER_NAME=platform-dev] [PROFILE=kind|full] [DEPLOY_VALUES=<yaml>]' \
	  'test-integration [CLUSTER_NAME=platform-dev] (uses an existing cluster)' \
	  'teardown [CLUSTER_NAME=platform-dev] (owned local kind cluster only)'

setup:
	./scripts/dev-setup.sh

typecheck:
	$(RUN) uv run --locked pyrefly check --config pyproject.toml
	$(RUN) pnpm --filter platform-ui typecheck

build-frontend:
	$(RUN) pnpm --filter platform-ui build

lint:
	$(RUN) prek run --all-files

format:
	$(RUN) prek run ruff-format --all-files

test: pytest-unit

pytest-unit:
	$(RUN) uv run --locked pytest tests/unit/ $(PYTEST_ARGS)

image-matrix:
	@$(RUN) uv run --no-project scripts/images.py --format matrix

cluster-up:
	./scripts/deploy.sh cluster-up

cluster-down: teardown

check-cluster:
	./scripts/deploy.sh check-cluster

deploy:
	./scripts/deploy.sh deploy

teardown:
	./scripts/teardown.sh "$(CLUSTER_NAME)"

test-integration:
	./scripts/test-integration.sh "$(CLUSTER_NAME)" $(PYTEST_ARGS)

pytest-integration: check-cluster
	$(RUN) uv run --locked pytest tests/integration/ -n 0 $(PYTEST_ARGS)

lint-helm: check-helm-deps
	@for chart in charts/*/Chart.yaml; do \
	  $(RUN) helm lint "$${chart%/Chart.yaml}"; \
	done; \
	$(RUN) uv run --locked scripts/render_profiles.py > /dev/null

check-helm-deps:
	@if [[ ! -f charts/platform/charts/temporal-1.7.0.tgz ]]; then \
	  $(RUN) helm repo add temporal https://go.temporal.io/helm-charts --force-update; \
	fi
	$(RUN) helm dependency build charts/platform --skip-refresh

update-schemas:
	$(RUN) uv run --locked scripts/update-crds.py all

lint-checkov:
	$(RUN) uv run --locked scripts/checkov_profiles.py

lint-kube: lint-kubernetes

lint-kubernetes:
	@directory=$$($(RUN) uv run --locked scripts/render_profiles.py); \
	for profile in default kind substrate; do \
	  $(RUN) kube-linter lint "$$directory/$$profile.yaml" --config .kube-linter.yaml; \
	done

check-schema:
	@directory=$$($(RUN) uv run --locked scripts/render_profiles.py); \
	for profile in default kind substrate; do \
	  $(RUN) kubeconform -strict -summary -skip KataConfig \
	    -schema-location default \
	    -schema-location '.schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
	    "$$directory/$$profile.yaml"; \
	done

scan-iac:
	$(RUN) trivy fs --config trivy.yaml --scanners misconfig,secret .

scan-secrets:
	$(RUN) gitleaks git --log-opts="HEAD" --redact --config .gitleaks.toml

scan-image:
	@[[ -n "$${IMAGE_REF:-}" ]] || { printf 'ERROR: Set IMAGE_REF=<image:tag>\n' >&2; exit 1; }
	$(RUN) trivy image --config trivy.yaml --scanners vuln,secret \
	  --format table --pkg-types library --severity HIGH,CRITICAL --exit-code 1 \
	  --skip-db-update=false --skip-java-db-update=false "$$IMAGE_REF"
	$(RUN) trivy image --config trivy.yaml --scanners vuln,secret \
	  --format table --pkg-types os --severity HIGH --exit-code 0 \
	  --skip-db-update=false --skip-java-db-update=false "$$IMAGE_REF"
	$(RUN) trivy image --config trivy.yaml --scanners vuln,secret \
	  --format table --pkg-types os --severity CRITICAL --exit-code 1 \
	  --skip-db-update=false --skip-java-db-update=false "$$IMAGE_REF"

scan-local:
	./scripts/run-all-local-scans.sh "$(if $(IMAGE),$(IMAGE),all)"

verify-actions-lock:
	gh actions-lock --verify --no-migrate-local-actions
