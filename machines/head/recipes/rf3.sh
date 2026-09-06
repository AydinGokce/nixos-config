# RF3: official Foundry code/checkpoint, with explicit prepared per-chain MSAs.
# The head prepares BIO_RF3_INPUT before renting the GPU; no worker-side search.
[ -n "${BIO_RF3_INPUT:-}" ] || {
  echo 'rf3: a verified prepared RF3 input is required; submit through bio-fold/bio-submit' >&2
  exit 2
}
[ -z "${BIO_MSA_BUNDLE:-}" ] && [ -z "${BIO_NATIVE_BUNDLE:-}" ] || {
  echo 'rf3: conflicting prepared input modes' >&2; exit 2
}
case "${MODEL:-}" in ''|rf3|latest) ;; *) echo 'rf3: only the pinned latest RF3 checkpoint is configured' >&2; exit 2 ;; esac
VENV="$SHARED/envs/rf3"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" "$TOOLS/rf3/runtime.py" environment \
    --shared "$SHARED" > "$OUT/rf3-environment.json"; then
  bash "$TOOLS/rf3/install.sh" "$SHARED"
  "$VENV/bin/python" "$TOOLS/rf3/runtime.py" environment --shared "$SHARED" > "$OUT/rf3-environment.json"
fi
export PATH="$VENV/bin:$PATH"
export LD_LIBRARY_PATH="$(venv_ld "$VENV")${LD_LIBRARY_PATH:-}"
"$VENV/bin/python" "$TOOLS/rf3/runtime.py" download --shared "$SHARED"
have_gpu
"$VENV/bin/python" "$TOOLS/rf3/runtime.py" predict --shared "$SHARED" \
  --input "$BIO_RF3_INPUT" --out "$OUT" -- "${EXTRA_ARGS[@]}"
