# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM ghcr.io/astral-sh/uv:0.12.23-trixie-slim@sha256:cb8750e12902fdaca60c1e48a796d19c02e6c2037ca801f322689fa97f0a44f2 AS robyn-wheel
# renovate: datasource=python-version depName=python
ARG PYTHON_VERSION=3.14.8
# TODO: Remove robyn-wheel stage once upstream sparckles/Robyn publishes >0.88.0 with PR #1422 cp314 aarch64 wheels.
ARG ROBYN_VERSION=0.88.0
ARG ROBYN_SHA256=3149c2c473b756f654424b00584c4563ce23c2d1595df44e0b09d30efc7c3781
ENV UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /tmp/robyn-build
# hadolint ignore=DL3008,DL4006
RUN uv python install "${PYTHON_VERSION}" && \
    mkdir -p /wheels && \
    if [ "$(uname -m)" = "aarch64" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends ca-certificates cargo curl gcc libc6-dev rustc && \
      rm -rf /var/lib/apt/lists/* && \
      curl -fsSL "https://github.com/sparckles/Robyn/archive/refs/tags/v${ROBYN_VERSION}.tar.gz" -o robyn.tar.gz && \
      printf '%s  robyn.tar.gz\n' "${ROBYN_SHA256}" | sha256sum -c - && \
      tar -xzf robyn.tar.gz --strip-components=1 && \
      CARGO_PROFILE_RELEASE_LTO=off CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
        uvx --from "maturin==1.10.2" maturin build --release --locked \
        --interpreter "$(uv python find "${PYTHON_VERSION}")" --out /wheels && \
      rm -rf /tmp/robyn-build /root/.cargo /root/.cache; \
    fi

FROM ghcr.io/astral-sh/uv:0.12.23-trixie-slim@sha256:cb8750e12902fdaca60c1e48a796d19c02e6c2037ca801f322689fa97f0a44f2 AS builder
# renovate: datasource=python-version depName=python
ARG PYTHON_VERSION=3.14.8
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /app
COPY --from=robyn-wheel /opt/python /opt/python
COPY --from=robyn-wheel /wheels /wheels
COPY pyproject.toml uv.lock ./
COPY apps/core/pyproject.toml ./apps/core/pyproject.toml
COPY apps/sandbox-exec/pyproject.toml ./apps/sandbox-exec/pyproject.toml
COPY libs/contracts/pyproject.toml ./libs/contracts/pyproject.toml
RUN if [ "$(uname -m)" = "aarch64" ]; then \
      uv sync --locked --python "${PYTHON_VERSION}" --package agent-director-sandbox-exec --no-install-workspace --no-dev --no-install-package robyn && \
      uv pip install --python /app/.venv/bin/python --no-deps /wheels/robyn-*.whl; \
    else \
      uv sync --locked --python "${PYTHON_VERSION}" --package agent-director-sandbox-exec --no-install-workspace --no-dev; \
    fi && \
    rm -rf /opt/python/*/lib/python*/site-packages/pip* /opt/python/*/lib/python*/site-packages/setuptools* /opt/python/*/bin/pip*

FROM gcr.io/distroless/cc-debian13@sha256:159783207c2cd44c2aa5715961d13c8612368ac9bd450f887e3f08fc8ea461e3
WORKDIR /app
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
COPY apps/sandbox-exec/sandbox_exec /app/sandbox_exec
COPY libs/contracts/contracts /app/contracts
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 ROBYN_CLI=true
USER 65532:65532

# Keep the container running using native Python (no shell available in distroless)
ENTRYPOINT ["python", "-c", "import robyn, sandbox_exec.daemon, time; print('Microservice stub running...', flush=True); time.sleep(3600000)"]
