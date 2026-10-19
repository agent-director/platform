"""In-sandbox Robyn execution and workspace hydration daemon."""

import hmac
import os
import sys
from collections.abc import Mapping

from pydantic import ValidationError

from contracts import HealthResponse, HydrateRequest, HydrateResponse

os.environ.setdefault("ROBYN_CLI", "true")
_saved_argv = sys.argv[:]
try:
    sys.argv = sys.argv[:1]
    from robyn import Request, Response, Robyn, status_codes
finally:
    sys.argv = _saved_argv

app = Robyn(__file__)


def verify_bearer_token(auth_header: str | None, expected_token: str) -> bool:
    """Validate Authorization: Bearer <token> in constant time, failing closed on empty tokens."""
    normalized_expected = expected_token.strip()
    if not normalized_expected or auth_header is None:
        return False
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return False
    candidate = auth_header[len(prefix) :]
    if not candidate or not candidate.strip():
        return False
    return hmac.compare_digest(
        candidate.encode("utf-8"),
        normalized_expected.encode("utf-8"),
    )


def build_readyz_response() -> Response:
    """Build the readiness probe response."""
    return Response(
        status_code=status_codes.HTTP_200_OK,
        headers={"content-type": "application/json"},
        description=HealthResponse().model_dump_json(),
    )


def handle_hydrate(
    body: str | bytes,
    headers: Mapping[str, str],
    environ: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    """Authenticate and validate a workspace hydration request."""
    env = os.environ if environ is None else environ
    expected_token = env.get("SANDBOX_AUTH_TOKEN", "")
    auth_header = headers.get("authorization") or headers.get("Authorization")
    if not verify_bearer_token(auth_header, expected_token):
        return (status_codes.HTTP_401_UNAUTHORIZED, '{"detail":"Unauthorized"}')
    try:
        raw_body = body.decode("utf-8") if isinstance(body, bytes) else body
        payload = HydrateRequest.model_validate_json(raw_body)
    except (UnicodeDecodeError, ValidationError):
        return (
            status_codes.HTTP_422_UNPROCESSABLE_ENTITY,
            '{"detail":"Invalid hydrate request"}',
        )
    response = HydrateResponse(
        workspace_id=payload.workspace_id,
        hydrated=True,
        checkout_path=f"/workspace/{payload.workspace_id}",
    )
    return (status_codes.HTTP_200_OK, response.model_dump_json())


@app.get("/readyz")
async def readyz() -> Response:
    return build_readyz_response()


@app.post("/hydrate")
async def hydrate(request: Request) -> Response:
    raw_headers: dict[str, str] = {}
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if isinstance(auth, str):
        raw_headers["authorization"] = auth
    body = request.body if isinstance(request.body, (str, bytes)) else str(request.body)
    code, payload = handle_hydrate(body, raw_headers)
    return Response(
        status_code=code,
        headers={"content-type": "application/json"},
        description=payload,
    )
