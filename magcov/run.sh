#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
REDIS_URL="${REDIS_URL:-redis://172.17.0.1:6379/0}"
JOB_NAMESPACE="${JOB_NAMESPACE:-libsndfile-directed-test}"

for required_script in run_worker.py execute_job.py process_coverage.py; do
  if [[ ! -f "${SCRIPT_DIR}/${required_script}" ]]; then
    echo "Required container script is missing: ${SCRIPT_DIR}/${required_script}" >&2
    exit 2
  fi
done

export PYTHON_DONT_WRITE_BYTECODE="${PYTHON_DONT_WRITE_BYTECODE:-1}"
export PYTHON_UNBUFFERED="${PYTHON_UNBUFFERED:-1}"

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/run_worker.py" \
  --redis-url "${REDIS_URL}" \
  --namespace "${JOB_NAMESPACE}" \
  --execute-script "${SCRIPT_DIR}/execute_job.py" \
  "$@"
