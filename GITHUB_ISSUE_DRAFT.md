# Infinite Hang/Deadlock in `pgxpool` Connect when Initial TLS Handshake Fails

## Description
In `agent-substrate` (`v0.1.0` and `v0.3.0`), the `ateapi` component hangs infinitely (until the context deadline is exceeded) during the initial PostgreSQL connection (`pool.Ping(ctx)`) when deployed against databases with self-signed certificates (e.g., Zalando Postgres Operator) or under environments where the initial connection attempt fails.

## Symptoms
- The `ateapi` pod logs `"ateapi starting"` and `"Configured JWT provider"`, then hangs silently for exactly the duration of the startup context (30 seconds).
- It then logs:
  ```
  "Failed to connect to PostgreSQL, retrying...", "attempt":1, "err":"PostgreSQL is unavailable: pinging PostgreSQL: context canceled"
  ```
- The `ateapi` container is eventually killed by Kubernetes liveness probes due to the infinite block.

## Root Cause Analysis
This is an infinite loop inside the `pgx` library's connection fallback mechanism, caused by a mutation in the `BeforeConnect` hook located in `pkg/atepg/atepg.go`:

```go
	cfg.BeforeConnect = func(_ context.Context, cc *pgx.ConnConfig) error {
		fresh, err := pgx.ParseConfig(dsn)
		if err != nil {
			return err
		}
		cc.TLSConfig = fresh.TLSConfig
		cc.Fallbacks = fresh.Fallbacks // <--- THE BUG IS HERE
		return nil
	}
```

When `pgx` attempts to dial a connection with `sslmode=prefer` or `sslmode=require`, it configures `Fallbacks` (e.g., first try TLS, then try plain TCP).
When the first connection attempt fails (e.g., due to Go's `crypto/tls` rejecting Spilo's self-signed cert, or an MTU packet drop during the TLS 1.3 handshake), `pgx` catches the error and iterates to the next fallback configuration.

However, `pgx` invokes `BeforeConnect` **for every fallback iteration**. Because the hook parses the DSN from scratch and overwrites `cc.Fallbacks` with the original slice (`fresh.Fallbacks`), the fallback iteration state is corrupted/reset.
This causes `pgx` to retry the exact same failing connection configuration in an infinite loop, never properly falling back, and never returning an error until the parent context is explicitly canceled.

## Environment
- **Go Version:** 1.24+ (which introduces stricter ML-KEM TLS handshake sizes, exacerbating connection failures on `kind` overlay networks).
- **Postgres:** Zalando Postgres Operator (Spilo) providing self-signed certificates.
- **pgx:** `v5.10.0`

## Workarounds
- Recompiling `agent-substrate` with the `cc.Fallbacks = fresh.Fallbacks` line removed from `BeforeConnect` immediately resolves the infinite loop, allowing `pgx` to either gracefully fall back to plain TCP or immediately bubble up the "certificate signed by unknown authority" error instead of deadlocking.
