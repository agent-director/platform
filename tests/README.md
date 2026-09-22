# Testing Contract

This project defines three tiers of tests to guarantee code quality and stability.

## Test Tiers

1. **Unit Tests (`tests/unit/`)**
   - **Scope:** Fast, deterministic testing of pure logic.
   - **Environment:** No live cluster components.
   - **Integration:** Uses `app.dependency_overrides` for dependency injection.

2. **Integration Tests (`tests/integration/`)**
   - **Scope:** Verifies interaction with real components.
   - **Environment:** Connects to real Postgres and Temporal running in the Kind cluster.
   - **Network Routing:** Routed via `/etc/hosts` NodePort mappings to ensure realistic behavior.

3. **End-to-End Tests (`tests/e2e/`)**
   - **Scope:** Verifies the full system by executing tasks through a real workspace.

## Determinism & Quarantine
Tests must be fully deterministic. Flaky tests that fail sporadically must be quarantined using `@pytest.mark.quarantine` until fixed. The CI workflow is configured to ignore the resulting exit code 5 (no tests collected) if a specific test run isolates all matching tests into quarantine.
