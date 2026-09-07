# A worker keeps the existing absolute venv/checkpoint paths, with writable
# package installs, source checkouts and model caches private to its own disk.
# The head holds shared leases on both legacy submission locks for this entire
# worker lifetime, excluding legacy writers and operator cache maintenance.
# Run outputs and inference/MSA result caches remain on the persistent NFS.
bio_worker_isolate_runtime() {
  local shared="$1" plan="$2" parent="${3:-/var/lib}" relative target roots index=0
  BIO_WORKER_MOUNTS=()
  BIO_WORKER_SCRATCH=$(mktemp -d "$parent/bio-runtime.XXXXXXXX")
  # Managed NFS advertises an unsupported system.nfs4_acl attribute, so kernel
  # and FUSE overlay copy-up both fail. Copy only this model's selected assets,
  # then bind them over the original paths: venv shebangs and editable imports
  # continue to resolve exactly as before, while package writes stay local.
  python3 "$BIO_TOOLS_DIR/py/worker_runtime.py" stage --shared "$shared" \
    --plan "$plan" --destination "$BIO_WORKER_SCRATCH/root"
  roots=$(python3 "$BIO_TOOLS_DIR/py/worker_runtime.py" roots)
  while IFS= read -r relative; do
    target="$shared/$relative"
    [ "$(realpath -m "$target")" = "$target" ] || { echo "bio-submit: runtime root contains a symlink: $target" >&2; return 1; }
    mkdir -p "$target"
    mount --bind "$BIO_WORKER_SCRATCH/root/$relative" "$target"
    BIO_WORKER_MOUNTS+=("$target")
    index=$((index + 1))
  done <<< "$roots"
  export UV_LINK_MODE=copy
  echo "bio-submit: isolated $index runtime/cache trees using selected assets on worker-local disk"
}

bio_worker_cleanup_runtime() {
  local index failed=0
  for ((index=${#BIO_WORKER_MOUNTS[@]}-1; index>=0; index--)); do
    umount -- "${BIO_WORKER_MOUNTS[index]}" || failed=1
  done
  # Never recurse through a mount if an unmount failed. The head still
  # deletes this owned VM; retaining its scratch directory here is harmless.
  if [ "$failed" = 0 ] && [ -n "${BIO_WORKER_SCRATCH:-}" ]; then
    rm -rf -- "$BIO_WORKER_SCRATCH"
  fi
  return "$failed"
}
