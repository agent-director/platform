#!/usr/bin/env bash
# Container vulnerability and secret scanning helper for scan-image and scan-local.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

MISE="${MISE:-mise}"
run() { "$MISE" exec -- "$@"; }

scan_image() {
    local image_ref="${IMAGE_REF:-${1:-}}"
    [[ -n "$image_ref" ]] || {
        printf 'ERROR: Set IMAGE_REF=<image:tag>\n' >&2
        exit 1
    }

    mkdir -p .cache/trivy/reports
    local full_report lib_report os_report
    full_report="$(mktemp .cache/trivy/reports/full.XXXXXXXX.json)"
    lib_report="$(mktemp .cache/trivy/reports/lib.XXXXXXXX.json)"
    os_report="$(mktemp .cache/trivy/reports/os.XXXXXXXX.json)"
    trap "rm -f '$full_report' '$lib_report' '$os_report'" EXIT

    local extra_flags=()
    if [[ -n "${TRIVY_IMAGE_FLAGS:-}" ]]; then
        read -r -a extra_flags <<< "$TRIVY_IMAGE_FLAGS"
    fi

    run trivy image --config trivy.yaml ${extra_flags[@]+"${extra_flags[@]}"} --scanners vuln,secret \
        --pkg-types os,library --severity HIGH,CRITICAL --exit-code 0 \
        --format json --output "$full_report" "$image_ref"

    run jq 'if .Results then .Results |= map(select(.Class != "os-pkgs")) else . end' "$full_report" > "$lib_report"
    run jq 'if .Results then .Results |= map(select(.Class == "os-pkgs")) else . end' "$full_report" > "$os_report"

    run trivy convert --config trivy.yaml --scanners vuln,secret \
        --format table --severity HIGH,CRITICAL --exit-code 1 "$lib_report"
    run trivy convert --config trivy.yaml --scanners vuln \
        --format table --severity HIGH --exit-code 0 "$os_report"
    run trivy convert --config trivy.yaml --scanners vuln \
        --format table --severity CRITICAL --exit-code 1 "$os_report"
}

image_affected() {
    local name="$1" dockerfile="$2" diff_files="$3"
    local component="${dockerfile#images/}"
    component="${component%/Dockerfile}"
    [[ -n "$diff_files" ]] || return 1

    if grep -qE '^(mise\.toml|mise\.lock|trivy\.yaml|\.trivyignore\.yaml|\.dockerignore)$' <<< "$diff_files"; then
        return 0
    fi
    if grep -qE "^images/$component/" <<< "$diff_files"; then
        return 0
    fi
    case "$name" in
        unified-api)
            grep -qE '^(pyproject\.toml|uv\.lock|apps/core/pyproject\.toml|apps/sandbox-exec/pyproject\.toml|apps/core/unified_api/|libs/contracts/)' <<< "$diff_files" && return 0
            ;;
        platform-worker)
            grep -qE '^(pyproject\.toml|uv\.lock|apps/core/pyproject\.toml|apps/sandbox-exec/pyproject\.toml|apps/core/orchestrator/|libs/contracts/)' <<< "$diff_files" && return 0
            ;;
        sandbox)
            grep -qE '^(pyproject\.toml|uv\.lock|apps/core/pyproject\.toml|apps/sandbox-exec/|libs/contracts/)' <<< "$diff_files" && return 0
            ;;
        frontend)
            grep -qE '^(package\.json|pnpm-workspace\.yaml|pnpm-lock\.yaml|apps/ui/)' <<< "$diff_files" && return 0
            ;;
    esac
    return 1
}

scan_local() {
    local selection="${IMAGE:-${1:-affected}}"
    local diff_files=""
    if [[ "$selection" == "affected" ]]; then
        local base_ref
        base_ref="$(git rev-parse --verify -q upstream/main 2>/dev/null || git rev-parse --verify -q origin/main 2>/dev/null || git rev-parse --verify -q @{upstream} 2>/dev/null || true)"
        if [[ -n "$base_ref" ]]; then
            diff_files="$( { git diff --name-only "$base_ref"...HEAD 2>/dev/null || true; git status --porcelain | awk '{print $NF}'; } | sort -u )"
        else
            selection="all"
        fi
    fi

    local targets=()
    local matched=0
    local name dockerfile target component
    while IFS=$'\t' read -r name dockerfile target; do
        [[ -n "$name" ]] || continue
        component="${dockerfile#images/}"
        component="${component%/Dockerfile}"
        if [[ "$selection" == "all" || "$selection" == "$name" || "$selection" == "$component" || "$selection" == "images/$component" ]]; then
            matched=1
            targets+=("$name|$dockerfile|$target")
        elif [[ "$selection" == "affected" ]]; then
            matched=1
            if image_affected "$name" "$dockerfile" "$diff_files"; then
                targets+=("$name|$dockerfile|$target")
            fi
        fi
    done < <(run uv run --no-project tools/scripts/images.py --format tsv)

    [[ "$matched" -eq 1 ]] || {
        printf 'ERROR: Unknown image selection: %s\n' "$selection" >&2
        exit 2
    }
    if [[ "${#targets[@]}" -eq 0 ]]; then
        printf 'No image or runtime dependency files modified; skipping local container scans.\n'
        exit 0
    fi
    if [[ ! -f .cache/trivy/db/trivy.db ]]; then
        run trivy image --config trivy.yaml --download-db-only
    fi

    mkdir -p .cache/trivy/local-scans .cache/trivy/workers
    rm -f .cache/trivy/local-scans/*.log .cache/trivy/local-scans/*.status
    local pids=()
    local entry pid
    for entry in "${targets[@]}"; do
        IFS='|' read -r name dockerfile target <<< "$entry"
        (
            set -euo pipefail
            local tag="local-scan/$name:latest"
            local target_args=()
            if [[ -n "$target" ]]; then
                target_args=(--target "$target")
            fi
            local worker_cache=".cache/trivy/workers/$name"
            mkdir -p "$worker_cache"
            ln -sfn "$ROOT/.cache/trivy/db" "$worker_cache/db"
            if [[ -d "$ROOT/.cache/trivy/java-db" ]]; then
                ln -sfn "$ROOT/.cache/trivy/java-db" "$worker_cache/java-db"
            fi
            run docker buildx build -f "$dockerfile" --load ${target_args[@]+"${target_args[@]}"} -t "$tag" .
            IMAGE_REF="$tag" TRIVY_IMAGE_FLAGS="--cache-dir $worker_cache --skip-db-update --skip-java-db-update" scan_image
        ) > ".cache/trivy/local-scans/$name.log" 2>&1 &
        pid="$!"
        pids+=("$pid:$name")
        if [[ "${CI:-false}" == "true" ]]; then
            wait "$pid" || true
        fi
    done

    local failures=0 item
    for item in "${pids[@]}"; do
        pid="${item%%:*}"
        name="${item#*:}"
        if wait "$pid"; then
            printf 'PASS %-28s\n' "$name"
        else
            printf 'FAIL %-28s\n' "$name" >&2
            cat ".cache/trivy/local-scans/$name.log" >&2
            failures=$((failures + 1))
        fi
    done
    [[ "$failures" -eq 0 ]] || exit 1
}

cmd="${1:-}"
shift || true
case "$cmd" in
    scan-image)
        scan_image "$@"
        ;;
    scan-local)
        scan_local "$@"
        ;;
    *)
        printf 'Usage: tools/scripts/scan-images.sh scan-image|scan-local [args...]\n' >&2
        exit 2
        ;;
esac
