SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

MISE := ./scripts/mise.sh
RUN := $(MISE) exec --
CLUSTER_NAME ?=
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

.PHONY: help setup check-toolchain typecheck lint format test pytest-unit
.PHONY: cluster-up cluster-down check-cluster deploy teardown test-integration pytest-integration
.PHONY: lint-helm lint-checkov lint-kubernetes lint-kube check-helm-deps update-schemas check-schema scan-iac scan-secrets scan-local

help:
	@printf '%s\n' 'setup / check-toolchain / typecheck / lint / format / pytest-unit' \
	  'cluster-up CLUSTER_NAME=<unique-name> [REUSE_CLUSTER=true]' \
	  'deploy CLUSTER_NAME=<name> PROFILE=kind|full [DEPLOY_VALUES=<yaml>]' \
	  'test-integration CLUSTER_NAME=<name> (uses an existing cluster)' \
	  'teardown CLUSTER_NAME=<owned-name> (local kind cluster only)'

setup:
	./scripts/dev-setup.sh

check-toolchain:
	$(RUN) uv run --locked scripts/toolchain.py check

typecheck:
	$(RUN) uv run --locked pyrefly check --config pyproject.toml

lint:
	$(RUN) prek run --all-files

format:
	$(RUN) prek run ruff-format --all-files

test: pytest-unit

pytest-unit:
	$(RUN) uv run --locked pytest tests/unit/ $(PYTEST_ARGS)

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
	$(RUN) uv run --locked scripts/helm_dependencies.py build

update-schemas: check-helm-deps
	$(RUN) uv run --locked scripts/update-crds.py all

# Setup materializes dependencies once. Offline gates validate locks and render
# independently into unique cache directories, without concurrent chart/repo writes.
lint-checkov:
	$(RUN) prek exec checkov -- uv run --locked scripts/checkov_profiles.py

lint-kube: lint-kubernetes

lint-kubernetes:
	@directory=$$($(RUN) uv run --locked scripts/render_profiles.py); \
	for profile in default kind bootstrap full substrate; do \
	  $(RUN) prek exec kube-linter -- kube-linter lint \
	    "$$directory/$$profile.yaml" --config .kube-linter.yaml; \
	done; \
	$(RUN) prek exec kube-linter -- kube-linter lint \
	  "$$directory/operator.yaml" --config .kube-linter-operator.yaml

check-schema:
	@directory=$$($(RUN) uv run --locked scripts/render_profiles.py); \
	for profile in default kind bootstrap full substrate operator; do \
	  $(RUN) kubeconform -strict -summary -skip KataConfig \
	    -schema-location default \
	    -schema-location '.schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
	    "$$directory/$$profile.yaml"; \
	done

scan-iac:
	$(RUN) trivy fs --scanners misconfig,secret --severity HIGH,CRITICAL --config trivy.yaml --exit-code 1 .

scan-secrets:
	$(RUN) prek run gitleaks --all-files

scan-local:
	./scripts/run-all-local-scans.sh "$(if $(IMAGE),$(IMAGE),all)"
