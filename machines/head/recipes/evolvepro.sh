# EVOLVEpro-style directed evolution: ESM-2 embeddings -> regression -> candidates.
# IN contains all measured + candidate variants; LABELS is an optional CSV with
# variant,activity. SUB=rank (default) or embed. MODEL selects the embedding PLM;
# NUM chooses the next-round size; EXTRA_ARGS accepts --regressor and --seed.
JOB="$SHARED/tools/py/evolvepro_cloud.py"
args=(--input "$IN" --out "$OUT" --embedding-model "${MODEL:-esm2_t33_650M_UR50D}"
      --num "${NUM:-12}" --sub "${SUB:-rank}")
[ -n "${LABELS:-}" ] && args+=(--labels "$LABELS")
set -- "${EXTRA_ARGS[@]}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --regressor|--seed)
      [ "$#" -ge 2 ] || { echo "bio-evolvepro: $1 needs a value" >&2; exit 2; }
      args+=("$1" "$2"); shift 2 ;;
    *) echo "bio-evolvepro: unsupported extra option '$1' (use --regressor or --seed)" >&2; exit 2 ;;
  esac
done
python3 "$JOB" "${args[@]}" --validate-only

PLM="$SHARED/envs/evolvepro-plm"
CORE="$SHARED/envs/evolvepro-core"
sys_venv "$PLM"
uv pip install --python "$PLM/bin/python" -r "$SHARED/tools/requirements/evolvepro-plm.txt"
if [ "${SUB:-rank}" = rank ]; then
  sys_venv "$CORE"
  uv pip install --python "$CORE/bin/python" -r "$SHARED/tools/requirements/evolvepro-core.txt"
fi
export LD_LIBRARY_PATH="$(venv_ld "$PLM")${LD_LIBRARY_PATH:-}"
have_gpu
python3 "$JOB" "${args[@]}" \
  --plm-python "$PLM/bin/python" --core-python "$CORE/bin/python"
