"""Exercise the rewritten runtime test's failure output using nonfunctional data."""

import subprocess
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from tests.integration.conftest import ClusterContract, IntegrationEnvironment


@pytest.mark.parametrize("password_in_logs", [False, True])
@pytest.mark.parametrize("missing_marker", ["flags", "migrations"])
def test_substrate_startup_failures_never_include_password_in_pytest_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    password_in_logs: bool,
    missing_marker: str,
) -> None:
    pytest.register_assert_rewrite("tests.integration.test_smoke")
    from tests.integration import test_smoke

    assert type(test_smoke.__loader__).__name__ == "AssertionRewritingHook"
    password = "nonfunctional-" + uuid4().hex
    markers = {
        "flags": "Final flag values",
        "migrations": "PostgreSQL migrations ready",
    }
    logs = (
        "synthetic-log-"
        + uuid4().hex
        + "\n"
        + markers["migrations" if missing_marker == "flags" else "flags"]
    )
    if password_in_logs:
        logs += "\n" + password

    class Contract:
        substrate_release = "unit"
        substrate_namespace = "unit-substrate"

        def manifest(self, release: str, namespace: str) -> list[dict[str, Any]]:
            assert (release, namespace) == (
                self.substrate_release,
                self.substrate_namespace,
            )
            return [
                {
                    "kind": "Deployment",
                    "metadata": {"name": "unit-api"},
                    "spec": {
                        "selector": {
                            "matchLabels": {"app.kubernetes.io/component": "ateapi"}
                        },
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "args": [
                                            "--postgres-read-write-connection-string=$(POSTGRES_URL)",
                                            "--postgres-owner-connection-string=$(POSTGRES_URL)",
                                        ],
                                        "env": [
                                            {
                                                "name": "POSTGRES_URL",
                                                "value": "postgres://$(PGUSER)@unit-db/db",
                                            },
                                            {
                                                "name": "PGPASSWORD",
                                                "valueFrom": {
                                                    "secretKeyRef": {
                                                        "name": "unit-credential",
                                                        "key": "password",
                                                    }
                                                },
                                            },
                                        ],
                                    }
                                ]
                            }
                        },
                    },
                }
            ]

        def kubectl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
            assert arguments == (
                "logs",
                "deployment/unit-api",
                "--namespace",
                self.substrate_namespace,
            )
            return subprocess.CompletedProcess(arguments, 0, stdout=logs)

    def credentials(
        _contract: ClusterContract, namespace: str, name: str
    ) -> dict[str, str]:
        assert (namespace, name) == ("unit-substrate", "unit-credential")
        return {"password": password}

    monkeypatch.setattr(test_smoke, "secret_data", credentials)
    environment = cast(IntegrationEnvironment, SimpleNamespace(contract=Contract()))
    with pytest.raises(pytest.fail.Exception) as failure:
        test_smoke.test_substrate_database_password_stays_out_of_flags_and_logs(
            environment
        )
    diagnostic = str(failure.getrepr(showlocals=False))
    assert password not in diagnostic
    assert logs not in diagnostic
