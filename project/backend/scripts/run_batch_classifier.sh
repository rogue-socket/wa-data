#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT_DIR="$(cd "${BACKEND_DIR}/../.." && pwd)"

if [[ -f "${ROOT_DIR}/user.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/user.env"
  set +a
fi

if [[ "${GEMINI_BATCH_ENABLED:-true}" != "true" ]]; then
  echo "batch-classifier skipped: GEMINI_BATCH_ENABLED is not true"
  exit 0
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "batch-classifier skipped: GEMINI_API_KEY is empty"
  exit 0
fi

cd "${BACKEND_DIR}"
conda run -n wa-data python -m app.batch_classifier "$@"
