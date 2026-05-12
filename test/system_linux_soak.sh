#!/usr/bin/env bash
set -u
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "${PYTHON}" ]; then
  echo "python not found"
  exit 1
fi
if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found"
  exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  echo "git not found"
  exit 1
fi

RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
TEST_PARENT="$(cd "${ROOT}/.." && pwd)"
TEST_DIR="${TEST_PARENT}/test_ccb_linux_soak_${RUN_ID}"
PROJECT="${TEST_DIR}/project"
ARTIFACT_ROOT="${TEST_DIR}/artifacts"
HOME_DIR="${ARTIFACT_ROOT}/home"
STUB_BIN="${ARTIFACT_ROOT}/bin"
STUB_PROVIDER="${ROOT}/test/stubs/provider_stub.py"
CODEX_ROOT="${ARTIFACT_ROOT}/codex"
CLAUDE_ROOT="${ARTIFACT_ROOT}/claude"
GEMINI_ROOT="${ARTIFACT_ROOT}/gemini"
OPENCODE_ROOT="${ARTIFACT_ROOT}/opencode"
DROID_ROOT="${ARTIFACT_ROOT}/droid"
TMUX_SOCKET="ccb-linux-soak-${RUN_ID}"

SOAK_SECONDS="${CCB_LINUX_SOAK_SECONDS:-1800}"
KILL_EVERY="${CCB_LINUX_SOAK_KILL_EVERY:-5}"
MAX_RECEIPT_MS="${CCB_LINUX_SOAK_MAX_RECEIPT_MS:-2500}"
P95_RECEIPT_MS="${CCB_LINUX_SOAK_P95_RECEIPT_MS:-1500}"
STUB_DELAY="${CCB_LINUX_SOAK_STUB_DELAY:-0.4}"
ASK_WAIT_TIMEOUT_S="${CCB_LINUX_SOAK_ASK_WAIT_TIMEOUT_S:-90}"

mkdir -p "${PROJECT}/.ccb" "${HOME_DIR}" "${STUB_BIN}" "${CODEX_ROOT}" "${CLAUDE_ROOT}" "${GEMINI_ROOT}" "${OPENCODE_ROOT}" "${DROID_ROOT}"

export HOME="${HOME_DIR}"
export PATH="${STUB_BIN}:${PATH}"
export CODEX_SESSION_ROOT="${CODEX_ROOT}"
export CLAUDE_PROJECTS_ROOT="${CLAUDE_ROOT}"
export GEMINI_ROOT
export OPENCODE_STORAGE_ROOT="${OPENCODE_ROOT}"
export DROID_SESSIONS_ROOT="${DROID_ROOT}"
export CODEX_START_CMD="${STUB_BIN}/codex"
export GEMINI_START_CMD="${STUB_BIN}/gemini"
export CLAUDE_START_CMD="${STUB_BIN}/claude"
export CCB_TMUX_SOCKET="${TMUX_SOCKET}"
export CCB_REPLY_LANG="en"
export CCB_CLAUDE_SKILLS=0
export CCB_SYNC_TIMEOUT=30
export CCB_WAIT_TIMEOUT_S=45
export CCB_WAIT_POLL_INTERVAL_S=0.1
export CCB_ASK_WAIT_TIMEOUT_S="${ASK_WAIT_TIMEOUT_S}"
export CCB_ASK_WAIT_POLL_INTERVAL_S=0.1
export CCB_WATCH_TIMEOUT_S=45
export CCB_WATCH_POLL_INTERVAL_S=0.1
export CCB_GEMINI_READY_TIMEOUT_S=0.5
export CCB_CLAUDE_READY_TIMEOUT_S=0.5
export STUB_DELAY
unset CCB_SESSION_FILE

FAIL=0
SESSIONS=()
JOBS_FILE="${PROJECT}/jobs.txt"
LATENCY_FILE="${PROJECT}/submit-latencies-ms.txt"
METRICS_FILE="${PROJECT}/soak-metrics.txt"

log() { echo "== $*"; }
ok() { echo "[OK] $*"; }
fail() { echo "[FAIL] $*"; FAIL=1; }
has_match() {
  local text="$1"
  local pattern="$2"
  grep -q -- "${pattern}" <<<"${text}"
}

q() {
  printf '%q' "$1"
}

tmux_cmd() {
  tmux -L "${TMUX_SOCKET}" "$@"
}

cleanup() {
  if [ -d "${PROJECT}" ]; then
    env HOME="${HOME}" PATH="${PATH}" CCB_TMUX_SOCKET="${CCB_TMUX_SOCKET}" "${ROOT}/ccb" --project "${PROJECT}" kill >/dev/null 2>&1 || true
  fi
  local session
  for session in "${SESSIONS[@]}"; do
    tmux_cmd kill-session -t "${session}" >/dev/null 2>&1 || true
  done
  tmux_cmd kill-server >/dev/null 2>&1 || true
}
trap cleanup EXIT

install_stub_providers() {
  local provider
  for provider in codex gemini claude opencode droid; do
    cat >"${STUB_BIN}/${provider}" <<EOF
#!/usr/bin/env bash
exec "${PYTHON}" "${STUB_PROVIDER}" --provider ${provider} "\$@"
EOF
    chmod +x "${STUB_BIN}/${provider}"
  done
}

bootstrap_tmux_server() {
  local bootstrap="linux-soak-bootstrap-${RUN_ID}"
  tmux_cmd new-session -d -s "${bootstrap}" -c "${ARTIFACT_ROOT}" "sleep 7200"
  SESSIONS+=("${bootstrap}")
  tmux_cmd set-environment -g HOME "${HOME}"
  tmux_cmd set-environment -g PATH "${PATH}"
  tmux_cmd set-environment -g CODEX_SESSION_ROOT "${CODEX_SESSION_ROOT}"
  tmux_cmd set-environment -g CLAUDE_PROJECTS_ROOT "${CLAUDE_PROJECTS_ROOT}"
  tmux_cmd set-environment -g GEMINI_ROOT "${GEMINI_ROOT}"
  tmux_cmd set-environment -g OPENCODE_STORAGE_ROOT "${OPENCODE_STORAGE_ROOT}"
  tmux_cmd set-environment -g DROID_SESSIONS_ROOT "${DROID_SESSIONS_ROOT}"
  tmux_cmd set-environment -g CODEX_START_CMD "${CODEX_START_CMD}"
  tmux_cmd set-environment -g GEMINI_START_CMD "${GEMINI_START_CMD}"
  tmux_cmd set-environment -g CLAUDE_START_CMD "${CLAUDE_START_CMD}"
  tmux_cmd set-environment -g CCB_TMUX_SOCKET "${CCB_TMUX_SOCKET}"
  tmux_cmd set-environment -g CCB_GEMINI_READY_TIMEOUT_S "${CCB_GEMINI_READY_TIMEOUT_S}"
  tmux_cmd set-environment -g CCB_CLAUDE_READY_TIMEOUT_S "${CCB_CLAUDE_READY_TIMEOUT_S}"
  tmux_cmd set-environment -g STUB_DELAY "${STUB_DELAY}"
}

init_project() {
  (
    cd "${PROJECT}" || exit 1
    git init -q
    git config user.email "ccb@example.test"
    git config user.name "ccb-system"
    printf '%s\n' "# linux soak ${RUN_ID}" >README.md
    git add README.md
    git commit -qm "init"
  )
  cat >"${PROJECT}/.ccb/ccb.config" <<'EOF'
alpha:codex,beta:claude,gamma:gemini
EOF
}

ccb_project() {
  env HOME="${HOME}" PATH="${PATH}" CODEX_SESSION_ROOT="${CODEX_SESSION_ROOT}" CLAUDE_PROJECTS_ROOT="${CLAUDE_PROJECTS_ROOT}" GEMINI_ROOT="${GEMINI_ROOT}" OPENCODE_STORAGE_ROOT="${OPENCODE_STORAGE_ROOT}" DROID_SESSIONS_ROOT="${DROID_ROOT}" CODEX_START_CMD="${CODEX_START_CMD}" GEMINI_START_CMD="${GEMINI_START_CMD}" CLAUDE_START_CMD="${CLAUDE_START_CMD}" CCB_TMUX_SOCKET="${CCB_TMUX_SOCKET}" CCB_GEMINI_READY_TIMEOUT_S="${CCB_GEMINI_READY_TIMEOUT_S}" CCB_CLAUDE_READY_TIMEOUT_S="${CCB_CLAUDE_READY_TIMEOUT_S}" CCB_CLAUDE_SKILLS=0 CCB_REPLY_LANG=en "${ROOT}/ccb" --project "${PROJECT}" "$@"
}

start_project() {
  ccb_project >"${PROJECT}/start-$(date +%s%N).out" 2>"${PROJECT}/start-$(date +%s%N).err"
}

wait_for_mount() {
  local timeout="$1"
  local start out
  start="$(date +%s)"
  while [ "$(( $(date +%s) - start ))" -lt "${timeout}" ]; do
    out="$(ccb_project ps 2>/dev/null)" || out=""
    if has_match "${out}" '^ccbd_state: mounted$'; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

wait_for_unmounted() {
  local timeout="$1"
  local start out
  start="$(date +%s)"
  while [ "$(( $(date +%s) - start ))" -lt "${timeout}" ]; do
    out="$(ccb_project ping ccbd 2>/dev/null)" || out=""
    if has_match "${out}" '^mount_state: unmounted$'; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

extract_job_id() {
  awk '
    /^accepted job=/ {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^job=/) {
          job = $i
          sub(/^job=/, "", job)
          print job
          exit
        }
      }
    }
  '
}

now_ms() {
  "${PYTHON}" - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

submit_and_wait_one() {
  local iteration="$1"
  local target="$2"
  local sender="$3"
  local started ended elapsed out job_id wait_out pend_out
  started="$(now_ms)"
  out="$(ccb_project ask "${target}" from "${sender}" "soak-${iteration}-${target}")" || {
    printf '%s\n' "${out}" >"${PROJECT}/ask-${iteration}.err"
    fail "submit ${iteration} ${target}"
    return
  }
  ended="$(now_ms)"
  elapsed=$(( ended - started ))
  printf '%s\n' "${elapsed}" >>"${LATENCY_FILE}"
  job_id="$(printf '%s\n' "${out}" | extract_job_id | head -n 1)"
  printf '%s\n' "${out}" >"${PROJECT}/ask-${iteration}-${target}.out"
  if [ -z "${job_id}" ]; then
    fail "submit ${iteration} job id"
    return
  fi
  printf '%s %s %s\n' "${job_id}" "${target}" "${elapsed}" >>"${JOBS_FILE}"
  if [ "${elapsed}" -le "${MAX_RECEIPT_MS}" ]; then
    ok "submit ${iteration} ${target} receipt ${elapsed}ms"
  else
    fail "submit ${iteration} ${target} receipt ${elapsed}ms > ${MAX_RECEIPT_MS}ms"
  fi

  wait_out="$(ccb_project ask wait "${job_id}")" || {
    fail "ask wait ${iteration} ${target} ${job_id}"
    return
  }
  printf '%s\n' "${wait_out}" >"${PROJECT}/wait-${iteration}-${job_id}.out"
  if has_match "${wait_out}" '^watch_status: terminal$' && has_match "${wait_out}" '^status: completed$'; then
    ok "ask wait ${iteration} ${target}"
  else
    fail "ask wait ${iteration} ${target}"
  fi

  pend_out="$(ccb_project pend "${target}")" || {
    fail "pend ${iteration} ${target}"
    return
  }
  printf '%s\n' "${pend_out}" >"${PROJECT}/pend-${iteration}-${target}.out"
  if has_match "${pend_out}" 'observer_notice: weak observer surface'; then
    ok "pend ${iteration} ${target}"
  else
    fail "pend ${iteration} ${target}"
  fi
}

assert_doctor() {
  local iteration="$1"
  local out
  out="$(ccb_project doctor)" || {
    fail "doctor ${iteration}"
    return
  }
  printf '%s\n' "${out}" >"${PROJECT}/doctor-${iteration}.out"
  if has_match "${out}" '^ccbd_state: mounted$' && has_match "${out}" '^ccbd_health: healthy$'; then
    ok "doctor ${iteration}"
  else
    fail "doctor ${iteration}"
  fi
}

kill_and_restart() {
  local iteration="$1"
  if ccb_project kill >"${PROJECT}/kill-${iteration}.out" 2>"${PROJECT}/kill-${iteration}.err"; then
    ok "kill ${iteration}"
  else
    fail "kill ${iteration}"
    return
  fi
  if wait_for_unmounted 20; then
    ok "kill unmounted ${iteration}"
  else
    fail "kill unmounted ${iteration}"
  fi
  start_project
  if wait_for_mount 25; then
    ok "restart mounted ${iteration}"
  else
    fail "restart mounted ${iteration}"
  fi
}

latency_percentile() {
  local percentile="$1"
  "${PYTHON}" - "${LATENCY_FILE}" "${percentile}" <<'PY'
import math
import sys
from pathlib import Path

values = sorted(int(line.strip()) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip())
if not values:
    print(0)
    raise SystemExit
p = float(sys.argv[2])
index = max(0, min(len(values) - 1, math.ceil((p / 100.0) * len(values)) - 1))
print(values[index])
PY
}

write_summary() {
  local p50 p95 max count
  count="$(wc -l <"${LATENCY_FILE}" | tr -d ' ')"
  p50="$(latency_percentile 50)"
  p95="$(latency_percentile 95)"
  max="$(latency_percentile 100)"
  {
    echo "soak_seconds: ${SOAK_SECONDS}"
    echo "iterations: ${count}"
    echo "submit_latency_p50_ms: ${p50}"
    echo "submit_latency_p95_ms: ${p95}"
    echo "submit_latency_max_ms: ${max}"
  } | tee "${METRICS_FILE}"
  if [ "${count}" -gt 0 ] && [ "${p95}" -le "${P95_RECEIPT_MS}" ]; then
    ok "soak p95 bounded ${p95}ms"
  else
    fail "soak p95 ${p95}ms > ${P95_RECEIPT_MS}ms"
  fi
}

run_soak() {
  local start now iteration target sender
  start="$(date +%s)"
  iteration=0
  log "Linux S1 soak: seconds=${SOAK_SECONDS} kill_every=${KILL_EVERY} stub_delay=${STUB_DELAY}s"
  while true; do
    now="$(date +%s)"
    if [ "$(( now - start ))" -ge "${SOAK_SECONDS}" ]; then
      break
    fi
    iteration=$(( iteration + 1 ))
    case $(( (iteration - 1) % 3 )) in
      0) target="alpha"; sender="user" ;;
      1) target="beta"; sender="alpha" ;;
      *) target="gamma"; sender="beta" ;;
    esac
    submit_and_wait_one "${iteration}" "${target}" "${sender}"
    assert_doctor "${iteration}"
    if [ "${KILL_EVERY}" -gt 0 ] && [ $(( iteration % KILL_EVERY )) -eq 0 ]; then
      kill_and_restart "${iteration}"
    fi
    if [ "${FAIL}" -ne 0 ]; then
      break
    fi
  done
  write_summary
}

: >"${JOBS_FILE}"
: >"${LATENCY_FILE}"
install_stub_providers
bootstrap_tmux_server
init_project
start_project
if wait_for_mount 25; then
  ok "project start"
else
  fail "project start"
fi

if [ "${FAIL}" -eq 0 ]; then
  run_soak
fi

if [ "${FAIL}" -ne 0 ]; then
  echo "artifacts: ${PROJECT}"
  echo "FAILURES DETECTED"
  exit 1
fi

echo "artifacts: ${PROJECT}"
echo "ALL TESTS PASSED"
