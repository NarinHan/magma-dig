#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "[build_target_cov] $*"
}

###############################################################################
# Time measurement helpers
###############################################################################

now_s() {
  date +%s
}

format_duration() {
  local elapsed="$1"

  printf '%02dh:%02dm:%02ds' \
    $((elapsed / 3600)) \
    $(((elapsed % 3600) / 60)) \
    $((elapsed % 60))
}

time_measure() {
  # Usage:
  #   time_measure "description" command [arguments...]

  local label="$1"
  shift

  local start
  local end
  local elapsed
  local formatted
  local rc

  log "BEGIN: $label"
  start="$(now_s)"

  "$@"
  rc=$?

  end="$(now_s)"
  elapsed=$((end - start))
  formatted="$(format_duration "$elapsed")"

  log "END:   $label (${formatted})"

  return "$rc"
}

###############################################################################
# Required environment variables
###############################################################################

: "${FUZZER_COV:?FUZZER_COV must be set, e.g., /magma/fuzzers/llvm_cov}"
: "${OUT_COV:?OUT_COV must be set}"
: "${PROGRAM:?PROGRAM must be set}"

###############################################################################
# Optional environment variables
###############################################################################

# Expected path of the coverage-instrumented executable.
COV_BIN="${COV_BIN:-$OUT_COV/$PROGRAM}"

# Set to 1 when the target has already been built.
SKIP_BUILD="${SKIP_BUILD:-0}"

log "starting build at $(date '+%F %T')"
log "FUZZER_COV=$FUZZER_COV"
log "OUT_COV=$OUT_COV"
log "PROGRAM=$PROGRAM"
log "COV_BIN=$COV_BIN"
log "SKIP_BUILD=$SKIP_BUILD"

###############################################################################
# Build and instrument the target
###############################################################################

run_build_steps() {
  if [[ "$SKIP_BUILD" == "1" ]]; then
    log "SKIP_BUILD=1; skipping build and instrumentation"
    return 0
  fi

  local build_script="$FUZZER_COV/build.sh"
  local instrument_script="$FUZZER_COV/instrument.sh"

  if [[ -x "$build_script" ]]; then
    time_measure \
      "run build.sh" \
      bash -c "cd '$FUZZER_COV' && ./build.sh"
  else
    log "ERROR: build script not found or not executable: $build_script"
    exit 2
  fi

  if [[ -x "$instrument_script" ]]; then
    time_measure \
      "run instrument.sh" \
      bash -c "cd '$FUZZER_COV' && ./instrument.sh"
  else
    log "ERROR: instrumentation script not found or not executable: $instrument_script"
    exit 2
  fi
}

run_build_steps

###############################################################################
# Verify the coverage-instrumented target
###############################################################################

if [[ ! -x "$COV_BIN" ]]; then
  log "ERROR: coverage-instrumented target was not found or is not executable"
  log "Expected binary: $COV_BIN"
  log "The target should be compiled with:"
  log "  -fprofile-instr-generate"
  log "  -fcoverage-mapping"
  exit 3
fi

log "coverage-instrumented target successfully built"
log "binary: $COV_BIN"
log "finished at $(date '+%F %T')"
