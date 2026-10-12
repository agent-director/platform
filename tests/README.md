# Testing Contract

This project defines two tiers of tests to guarantee code quality and stability.

## Test Tiers

1. **Unit Tests (`tests/unit/`)**
   - **Scope:** Fast, deterministic testing of pure logic.
   - **Environment:** No live cluster components.
   - **Integration:** Uses `app.dependency_overrides` for dependency injection.

2. **Integration Tests (`tests/integration/`)**
   - **Scope:** Verifies interaction with real components and the live cluster state.
   - **Environment:** Connects to real Postgres, Temporal, and the Unified API running in the Kind cluster.
   - **Network Routing:** Services are accessed via `kubectl port-forward` running natively.

## Determinism & Quarantine
Tests must be fully deterministic. If a test is flaky, it must be fixed or deleted; we do not maintain a quarantine graveyard.
