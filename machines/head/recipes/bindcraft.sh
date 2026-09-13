# Pinned environment/assets are prepared before rental and restored locally.
# All upstream inputs and output paths are materialized inside this job.
have_gpu
bindcraft_command=run
[ "${SUB:-}" != smoke ] || bindcraft_command=smoke
python3 "$TOOLS/bindcraft/runtime.py" "$bindcraft_command" --shared "$SHARED" --bundle "$IN" --out "$OUT"
