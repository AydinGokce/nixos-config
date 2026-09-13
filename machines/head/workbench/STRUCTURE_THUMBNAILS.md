# Protein gallery thumbnails

Gallery previews are disposable derivatives of retained PDB/mmCIF structures.
They use `bio-render-headless`, the same Rust studio renderer as the desktop.
They do not launch GPU workers, create model jobs, change library records, or
change the retained coordinates. A gallery can remain empty.

## Worker deployment

Install the Nix `apps/bio-workbench-rust/renderer-package.nix` package on the head
and run one service:

```sh
python3 /etc/bio-tools/workbench/library_structure_thumbnails.py \
  --state /var/lib/bio-workbench \
  --renderer /run/current-system/sw/bin/bio-render-headless
```

`--state` defaults to `BIO_WORKBENCH_STATE`, then `/var/lib/bio-workbench`.
`--renderer` defaults to `BIO_WORKBENCH_RENDERER`, then the path shown above.
RPC processes use that same environment variable/default path. The worker and
RPC must resolve it to the same executable. `--once` processes at most one
queued derivative and exits; it is intended for operator checks.

The worker writes only `<state>/structure-thumbnails/` and its temporary
directory. Recommended systemd settings are `UMask=0077`, `PrivateTmp=true`,
`ProtectSystem=strict`, `ProtectHome=true`, a writable cache subtree,
`CPUQuota=200%`, `MemoryMax=2G`, `TasksMax=64`, `KillMode=control-group`, and
`TimeoutStopSec=10`. Source directories require read access. The installed
head configuration supplies these settings.

The worker checks its queue every two seconds and holds an exclusive
cross-process lock while rendering. `LP_NUM_THREADS=2` and `OMP_NUM_THREADS=2`
bound software-renderer parallelism. Each renderer owns a separate process
group, gets no workbench actor/cloud credentials, and has a 60-second timeout.
Its children, including Xvfb, are terminated on completion or timeout.
Systemd control-group cleanup also covers an interrupted worker service.

## Limits and cache identity

- One render at a time; at most 16 queued or rendering entries.
- Source file at most 32 MiB; one temporary verified source copy per render.
- Cartoon PNG at 640 × 480, at most 2 MiB.
- At most 512 cache entries and 256 MiB of cached PNGs, evicted by recent use.
- Native renderer receipt at most 128 KiB; cache record at most 16 KiB.
- Failed renders have a five-minute cooldown before another visible-gallery
  request can enqueue them again. No background retry creates an endless loop.

The cache key binds the exact source SHA-256, source format, image dimensions,
style, fixed camera/title, and renderer fingerprint. The fingerprint includes
the resolved executable path, its content hash, and this worker module's hash.
For the Nix wrapper this binds the exact renderer and Mesa closure. Cargo's
unchanged `0.3.0` version alone is not used as a cache identity.

The service heartbeat distinguishes an unavailable worker from a queued
preview. Cached images can still be read when the worker is down. A changed
renderer fingerprint invalidates prior keys. Interrupted derivatives are
safe to render again; abandoned temporary copies are removed under the
exclusive worker lock. None of these operations remove the original source.

## RPC contract

`library.structure_thumbnail` accepts exactly `ref` and `entry_id`. The
reference is pinned. Both this call and every image read use
`library_structures.resolve_structure` to authorize the exact association and
verify the retained source. A cache hit does not bypass access checks.

The response contains:

```json
{
  "schema": 1,
  "ref": "construct:protein-id@1",
  "entry_id": "opaque-gallery-entry",
  "source_sha256": "64 lowercase hex digits",
  "renderer_fingerprint": "64 lowercase hex digits",
  "cache_key": "64 lowercase hex digits",
  "state": "ready",
  "width": 640,
  "height": 480,
  "style": "cartoon",
  "retry_after_seconds": 2,
  "sha256": "PNG SHA-256, 64 lowercase hex digits",
  "size": 123456
}
```

States are `queued`, `rendering`, `ready`, `failed`, `unavailable`, and `busy`.
Only `ready` carries PNG `sha256` and `size`. Errors include a short `error`
message. When the executable is absent, the fingerprint and key are `null`.

`library.structure_thumbnail_read` requires `ref`, `entry_id`, `cache_key`,
PNG `sha256`, `offset`, and `length` (1–262144 bytes). It returns the same ready
identity plus `offset`, `next_offset`, `eof`, and `data_base64`. Changed source,
renderer, receipt, expired cache, or authorization causes an error instead of
mixed image chunks. PNG hashes, fixed dimensions, chunk framing and CRCs are
verified before bytes are returned.

Desktop clients request only visible/current-page previews. They must verify
the complete receipt and every chunk before publishing an image to their
local cache, and make an authorized read before reusing a local image. Static,
bounded egui textures are appropriate for cards; a full molecular renderer per
card is unnecessary. Clicking a card opens its original verified structure in
the existing molecular viewer.

## Verification

```sh
cd machines/head
python3 -m unittest workbench.test_library_structure_thumbnails -v
```

The tests exercise source and image identity, authorization on cache hits,
renderer upgrades, interrupted derivatives, corrupt cache recovery, concurrent
deduplication, queue/entry limits, retry cooldown, and real renderer-process
group teardown with a harmless subprocess fixture. Production rendering can
be qualified with `--once` in a separate temporary state directory using a
retained test structure; it does not require model or cloud work.
