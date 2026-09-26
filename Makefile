SHELL := /bin/bash
.PHONY: cluster-up cluster-down setup deploy teardown test-e2e test lint format

NAMESPACE ?= gordon
RELEASE_NAME ?= agent-platform

GIT_SHA ?= $(shell git rev-parse HEAD)
IMAGE_TAG ?= main
CURRENT_API_URL = $(shell kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null)
# Auto-detect local cluster if present, otherwise require explicit input
KUBE_API_URL ?= $(if $(findstring 127.0.0.1,$(CURRENT_API_URL)),$(CURRENT_API_URL),$(if $(findstring localhost,$(CURRENT_API_URL)),$(CURRENT_API_URL),))


cluster-up:
	@echo "Creating local kind cluster..."
	@kind create cluster --name gordon-dev || true

cluster-down:
	@echo "Deleting local kind cluster..."
	@kind delete cluster --name gordon-dev || true

setup:
	./scripts/dev-setup.sh

check-cluster:
	@if [ -z "$(KUBE_API_URL)" ]; then \
		echo "ERROR: KUBE_API_URL is not set. Explicitly define it to prevent accidental deployments."; \
		echo "Current active API is: $(CURRENT_API_URL)"; \
		echo "Run: make [target] KUBE_API_URL=$(CURRENT_API_URL)"; \
		exit 1; \
	fi
	@if [ "$(KUBE_API_URL)" != "$(CURRENT_API_URL)" ]; then \
		echo "ERROR: Safety check failed!"; \
		echo "Active:   $(CURRENT_API_URL)"; \
		echo "Please log into the correct cluster to proceed."; \
		exit 1; \
	fi

deploy: setup check-cluster
	@echo "Deploying to cluster API: $(KUBE_API_URL)..."
	@# Install Zalando operator if not present
	@helm repo add postgres-operator-charts https://opensource.zalando.com/postgres-operator/charts/postgres-operator || true
	@helm upgrade --install postgres-operator postgres-operator-charts/postgres-operator \
		--namespace $(NAMESPACE) --create-namespace
	@kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	@if [ -f .env ]; then set -a; . .env; set +a; fi; \
	if [ -n "$$GH_PAT" ]; then \
		kubectl create secret docker-registry ghcr-secret \
			--namespace $(NAMESPACE) \
			--docker-server=ghcr.io \
			--docker-username=$${GH_USER:-gordon-control-plane} \
			--docker-password="$$GH_PAT" \
			--dry-run=client -o yaml | kubectl apply -f -; \
	fi; \
	if [ -n "$$TAILSCALE_AUTH_KEY" ]; then \
		kubectl create secret generic tailscale-auth \
			--namespace $(NAMESPACE) \
			--from-literal=TS_AUTHKEY="$$TAILSCALE_AUTH_KEY" \
			--dry-run=client -o yaml | kubectl apply -f -; \
	fi; \
	echo "Upgrading Helm chart (streaming live pod status)..."; \
	kubectl get pods -n $(NAMESPACE) -w & WATCH_PID=$$!; \
	helm upgrade --install $(RELEASE_NAME) charts/agent-platform \
		--namespace $(NAMESPACE) \
		--set tailscaleIngress.tailnet="$$TAILSCALE_DOMAIN" \
		--set tailscaleIngress.hostname="$(USER)-$(NAMESPACE)-$(RELEASE_NAME)" \
		--set tailscaleIngress.ephemeral=true \
		--set global.image.tag="$(IMAGE_TAG)" \
		--set secrets.langfuseNextauthSecret="$${LANGFUSE_NEXTAUTH_SECRET:-dummy}" \
		--set secrets.langfuseSalt="$${LANGFUSE_SALT:-dummy}" \
		$(HELM_ARGS) \
		--wait --timeout 600s; \
	HELM_EXIT=$$?; \
	kill $$WATCH_PID 2>/dev/null || true; \
	exit $$HELM_EXIT
teardown: check-cluster
	./scripts/teardown.sh $(NAMESPACE) $(RELEASE_NAME)

.PHONY: lint-helm
lint-helm:
	@echo "Linting Helm charts..."
	@for d in charts/*; do \
		if [ -d "$$d" ] && [ -f "$$d/Chart.yaml" ]; then \
			helm lint "$$d"; \
		fi; \
	done

.PHONY: check-schema
check-schema:
	@echo "Validating Helm schemas with Kubeconform..."
	@for d in charts/*; do \
		if [ -d "$$d" ] && [ -f "$$d/Chart.yaml" ]; then \
			helm template "$$(basename "$$d")" "$$d" \
				--set secrets.langfuseNextauthSecret="dummy" \
				--set secrets.langfuseSalt="dummy" \
				| kubeconform -strict -summary \
				-schema-location default \
				-schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'; \
		fi; \
	done

.PHONY: lint-kube
lint-kube:
	@echo "Running Kube-Linter..."
	kube-linter lint charts/ --config .kube-linter.yaml

.PHONY: scan-iac
scan-iac:
	@echo "Running Trivy IaC Scan..."
	trivy fs . --format table --exit-code 1 --severity CRITICAL,HIGH

test:
	uv run pytest tests/

test-e2e: check-cluster
	@echo "Running E2E tests against API: $(KUBE_API_URL)..."
	uv run pytest tests/e2e/

lint:
	uvx prek run --all-files

format:
	uvx ruff format .
