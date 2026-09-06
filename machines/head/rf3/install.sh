#!/usr/bin/env bash
# Isolated RF3 runtime; never edits another model's environment or databases.
set -euo pipefail
SHARED=${1:-/mnt/bio-shared}
PIN=b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c
SOURCE="$SHARED/src/foundry-rf3-$PIN"
VENV="$SHARED/envs/rf3"
STATE="$SHARED/rf3"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PATH=/root/.local/bin:$PATH
export UV_CACHE_DIR="$STATE/uv-cache"
# Managed NFS maps the worker root UID; scope Git trust to this exact checkout.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$SOURCE"
mkdir -p "$STATE/checkpoints" "$SHARED/src" "$SHARED/envs"
exec 9>"$STATE/install.lock"
flock 9
if [ ! -d "$SOURCE" ]; then
  git clone --depth 1 --branch production --no-checkout https://github.com/RosettaCommons/foundry.git "$SOURCE"
  git -C "$SOURCE" fetch --depth 1 origin "$PIN"
fi
git -C "$SOURCE" checkout --detach "$PIN"
[ "$(git -C "$SOURCE" rev-parse HEAD)" = "$PIN" ]
[ -z "$(git -C "$SOURCE" status --porcelain --untracked-files=no)" ]
if [ ! -e "$VENV" ]; then uv venv --python /usr/bin/python3 "$VENV"; fi
P="$VENV/bin/python"
"$P" -c 'import sys; assert sys.version_info[:2] == (3,12)'
[ -s "$SCRIPT_DIR/requirements.lock" ]
uv pip sync --python "$P" --require-hashes "$SCRIPT_DIR/requirements.lock" \
  --extra-index-url https://download.pytorch.org/whl/cu128 --index-strategy unsafe-best-match
uv pip install --python "$P" --no-deps "$SOURCE"
uv pip check --python "$P"
uv pip freeze --python "$P" > "$STATE/installed.txt"
"$P" - <<'PY'
import importlib.metadata as m, json, os
from pathlib import Path
root = Path(os.environ['UV_CACHE_DIR']).parent
versions = {name:m.version(name) for name in ('rc-foundry','atomworks','torch','cuequivariance','cuequivariance-torch','cuequivariance-ops-cu12','cuequivariance-ops-torch-cu12')}
print(json.dumps(versions,indent=2))
(root/'versions.json').write_text(json.dumps(versions,indent=2)+'\n')
PY
