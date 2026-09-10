#!/usr/bin/env bash
# Run the test suite from a local venv (created on first run).
#
#   ./run-tests.sh              unit + sandbox tests (no root)
#   sudo ./run-tests.sh --e2e   also the loop-device end-to-end test
#   ./run-tests.sh -k scanner   pass any extra args straight to pytest
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VENV=".venv"
if [ ! -x "${VENV}/bin/pytest" ]; then
    echo "--> creating ${VENV}"
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install --quiet --upgrade pip
    "${VENV}/bin/pip" install --quiet -r requirements-dev.txt
fi

ARGS=()
for a in "$@"; do
    case "$a" in
        --e2e) export BTRFS_RESTORE_E2E=1 ;;   # opt in to the loop-device test
        *) ARGS+=("$a") ;;
    esac
done

exec "${VENV}/bin/pytest" "${ARGS[@]}"
