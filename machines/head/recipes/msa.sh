# Full private ColabFold preparation on transient CPU compute. The database
# snapshot persists; this worker's localhost API is never exposed publicly.
msa_run() (
  set -euo pipefail
  [ "${#EXTRA_ARGS[@]}" -eq 0 ] || { echo 'msa: unexpected extra arguments' >&2; exit 2; }
  python3 - "$SUB" "$TOOLS/msa/search_profile.py" "$OUT" <<'PY'
import json, os, runpy, sys
from pathlib import Path
if sys.argv[1] == 'convert':
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    if int(memory['MemAvailable'].split()[0]) < 56*1024**2:
        raise SystemExit('msa: database conversion requires at least 56 GiB available RAM')
else:
    policy = runpy.run_path(sys.argv[2])
    profile = policy['resolve'](os.environ.get('BIO_MSA_SEARCH_PROFILE'))
    if sys.argv[1] == 'install' and profile['profile_id'] != policy['LEGACY_PROFILE']:
        raise SystemExit('msa: database installation requires the resident build profile')
    observed = policy['check_guest'](profile)
    limit = policy['check_cgroup'](profile)
    Path(sys.argv[3], 'memory-profile.json').write_text(json.dumps(
        dict(search_profile=profile, guest_memory=observed, memory_limit=limit), sort_keys=True)+'\n')
    print(f"MSA {profile['profile_id']}: {observed['available_bytes']/1024**3:.1f} GiB available RAM", flush=True)
PY
  source "$TOOLS/msa/tools.sh"
  if [ "$SUB" = session ]; then
    [ -n "${BIO_MSA_SESSION_ID:-}" ] || { echo 'msa: session ID is missing' >&2; exit 2; }
    exec python3 "$TOOLS/msa/session.py" serve \
      --session-id "$BIO_MSA_SESSION_ID" --state "/tmp/bio-msa-session-$BIO_MSA_SESSION_ID" \
      --out "$OUT" --database "$MSA_DB_ROOT" --tools "$TOOLS" --tools-root "$MSA_TOOLS_ROOT" \
      --results "$SHARED/cache/msa-api" --deadline "$BIO_JOB_DEADLINE_EPOCH" \
      --idle-seconds "${BIO_MSA_SESSION_IDLE_SECONDS:-900}" \
      --search-profile "$BIO_MSA_SEARCH_PROFILE" --warm "$BIO_MSA_SESSION_WARM"
  fi
  threads=$(nproc)
  if [ "$SUB" = convert ]; then
    [ "$threads" -le 8 ] || threads=8
    python3 "$TOOLS/msa/databases.py" convert --root "$MSA_DB_ROOT" \
      --tools-root "$MSA_TOOLS_ROOT" --threads "$threads" > "$OUT/database-conversion.json"
    exit 0
  fi
  [ "$threads" -le 64 ] || threads=64
  if [ "$SUB" = install ]; then
    python3 "$TOOLS/msa/databases.py" install --root "$MSA_DB_ROOT" \
      --tools-root "$MSA_TOOLS_ROOT" --threads "$threads" > "$OUT/database-install.json"
    python3 "$TOOLS/msa/databases.py" validate --root "$MSA_DB_ROOT" \
      --tools-root "$MSA_TOOLS_ROOT" > "$OUT/database-validation.json"
    [ -n "${BIO_MSA_PANEL_SHA256:-}" ] || exit 0
  fi
  # Keep every native/OpenMP execution limit in the same versioned profile.
  search_threads=$(python3 - "$TOOLS/msa/search_profile.py" "$BIO_MSA_SEARCH_PROFILE" <<'PY'
import runpy, sys
print(runpy.run_path(sys.argv[1])['resolve'](sys.argv[2])['mmseqs_threads'])
PY
  )
  export MMSEQS_NUM_THREADS="$search_threads"
  export OMP_NUM_THREADS="$search_threads" OMP_THREAD_LIMIT="$search_threads" OMP_DYNAMIC=FALSE
  python3 "$TOOLS/msa/server.py" config --root "$MSA_DB_ROOT" \
    --tools-root "$MSA_TOOLS_ROOT" --results "$SHARED/cache/msa-api" \
    --search-profile "$BIO_MSA_SEARCH_PROFILE" \
    --output "$OUT/msa-server.json" > "$OUT/server-command.json"
  "$MMSEQS_SERVER" -local -config "$OUT/msa-server.json" > "$OUT/msa-server.log" 2>&1 &
  server_pid=$!
  proxy_pid=""
  msa_stop() {
    local status=$?
    trap - EXIT
    if [ -n "$proxy_pid" ]; then
      kill "$proxy_pid" 2>/dev/null || true
      wait "$proxy_pid" 2>/dev/null || true
      python3 "$TOOLS/msa/server.py" export --audit "$OUT/api-audit" \
        --config "$OUT/msa-server.json" --output "$OUT/api-jobs" || { [ "$status" -ne 0 ] || status=1; }
    fi
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    exit "$status"
  }
  trap msa_stop EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  trap 'exit 129' HUP
  python3 - "$server_pid" <<'PY'
import os, socket, sys, time
pid = int(sys.argv[1])
for attempt in range(120):
    os.kill(pid, 0)
    try:
        with socket.create_connection(('127.0.0.1', 8080), timeout=1):
            break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit('msa: private API did not become ready')
PY
  if [ "$SUB" = serve ]; then
    echo 'msa: private API listening only on worker localhost:8080'
    wait "$server_pid"
    exit 0
  fi
  if [ -n "${BIO_MSA_PANEL_SHA256:-}" ]; then
    python3 "$TOOLS/msa/panel.py" run --manifest "$IN" --out "$OUT/panel" --tools "$TOOLS" \
      --expected-sha256 "$BIO_MSA_PANEL_SHA256" --deadline "$BIO_JOB_DEADLINE_EPOCH" \
      --config "$OUT/msa-server.json" --provenance "$OUT/msa-server.provenance.json"
    exit 0
  fi
  python3 "$TOOLS/msa/server.py" proxy --audit "$OUT/api-audit" > "$OUT/api-audit.log" 2>&1 &
  proxy_pid=$!
  python3 - "$proxy_pid" <<'PY'
import os, socket, sys, time
for attempt in range(30):
    os.kill(int(sys.argv[1]), 0)
    try:
        with socket.create_connection(('127.0.0.1', 8081), timeout=1):
            break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit('msa: API audit proxy did not become ready')
PY
  if [ "$MODEL" = rf3 ]; then
    python3 "$TOOLS/rf3/msa.py" search --queries "$IN" --out "$OUT/prepared" \
      --server-url http://127.0.0.1:8081 --source private \
      --database-provenance "$OUT/msa-server.provenance.json" --deadline "$BIO_JOB_DEADLINE_EPOCH"
    cp "$OUT/msa-server.provenance.json" "$OUT/preparation-provenance.json"
    exit 0
  fi
  case "$MODEL" in
    openfold3)
      venv="$SHARED/envs/openfold3"
      export OPENFOLD_CACHE="$SHARED/openfold3/home/.openfold3" ;;
    boltz2)
      venv="$SHARED/envs/boltz"
      export BOLTZ_CACHE="$SHARED/cache/boltz" ;;
    protenix)
      venv="$SHARED/envs/protenix"
      export PROTENIX_ROOT_DIR="$SHARED/protenix/release_data" ;;
    *) echo 'msa: invalid model for native preparation' >&2; exit 2 ;;
  esac
  [ -x "$venv/bin/python" ] || { echo "msa: missing pinned $MODEL environment at $venv" >&2; exit 2; }
  export PATH="$venv/bin:/usr/local/cuda/bin:$PATH"
  export LD_LIBRARY_PATH="$(venv_ld "$venv")${LD_LIBRARY_PATH:-}"
  "$venv/bin/python" "$TOOLS/msa/prepared.py" prepare --model "$MODEL" \
    --fasta "$IN" --out "$OUT/prepared" --server-url http://127.0.0.1:8081 --source private \
    --database-provenance "$OUT/msa-server.provenance.json"
  # Keep the server configuration and database identity alongside native inputs.
  cp "$OUT/msa-server.provenance.json" "$OUT/preparation-provenance.json"
)
msa_run
