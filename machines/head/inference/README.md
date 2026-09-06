# Resident folding execution

The head owns a durable SQLite queue. Model workers load one pinned configuration
once and process sequential requests. Different workers can run simultaneously;
CPU search/preparation and output scoring do not hold the prediction queue slot.

`bio-submit` and `bio-fold` accept `--execution auto|resident|ephemeral`. Auto uses
an active, audited profile when the input/options match. Protenix, OpenFold3 and
RF3 have native-default profiles. The qualified Boltz profile requires explicit
`-- --seed 42`; bare Boltz submissions preserve its native unspecified-seed
behavior on the ephemeral path. Other model overrides also use that configurable
path. `resident` fails if no
compatible profile exists. Once a request is queued, a timeout or worker failure
never silently launches another prediction or rental.

Operator commands on the head:

```sh
bio-inference workers
bio-inference list
bio-inference status REQUEST_ID
bio-inference enqueue --job prepared-request.json --wait
bio-inference retry FAILED_OR_INTERRUPTED_REQUEST_ID
bio-inference worker-start --target existing-allocation.json --config model.json --policy session.json
bio-submit boltz2 --fasta protein.fasta --execution resident -- --seed 42
```

`worker-start` uses an existing budget-managed allocation. It verifies provider,
OS, boot and GPU identity, refuses an occupied GPU, and respects the original
allocation deadline. Its idle timeout and systemd runtime limit are additional
bounds; `dc` remains responsible for allocation accounting and cleanup. It never
rents a VM implicitly. A process exception stops that worker generation so a
later job cannot inherit damaged native/CUDA state.

`model.json` binds model/checkpoint, full native configuration and a private
absolute scratch directory. `session.json` supplies a unique worker ID, physical
GPU UUID, absolute deadline, resource policy and a verified local runtime image
`{ "path": "/var/lib/bio-runtime-images/...", "sha256": "..." }`.
`runtime_image.py stage` copies an already verified venv without installing
packages, preserves package/source/system dependencies, and relocates declared
paths. Only verified immutable copies may share content through hardlinks.
Adapter source, runtime image, environment and resource settings are part of
the queue's configuration identity. Changing them creates a new generation.

Profiles in `/var/lib/bio-inference/profiles/MODEL.json` explicitly name the
`bio-submit-native-defaults-v1` interface (or Boltz's
`bio-submit-explicit-seed-v1` with `native_seed: 42`), configuration ID, seeds and
retained validation evidence (`validation_root`/`validation_files`). Publishing a profile
is an operator decision after native validation; an installed model alone does
not make a configuration eligible for automatic routing.

Prepared input caches bind exact input/chemistry, parser/adapter, search settings,
pairing/template behavior and database provenance. Public database versions that
the provider does not report are recorded as unknown. Cached searches are
replayed evidence, not new database queries or independent experiments. Every
request receives a writable materialization; mutable stochastic feature tensors
are recreated through the native model pipeline. The public backend remains the
default until the independent private/public quality comparison establishes the
intended level of equivalence.

RF3 checks its search cache before contacting an MSA service. Exact input bytes,
chemical assets, ordered chain queries, search/parser settings and the full
private database generation determine reuse. The public service's unreported
database version remains unknown. `--refresh-preparation` creates a new captured
search without overwriting the earlier evidence; explicit `--msa-bundle` inputs
replay the supplied capture. A separate request receipt distinguishes replay
from a new search. Expected chemistry is also prepared before GPU enqueue and
reused only with its pinned native runtime and helper sources.

On filesystems without atomic exclusive directory rename, publication first
claims a new directory with `mkdir` and retains an incomplete marker through
transfer and synchronization. Readers require the final cache receipt and exact
file and directory inventories, including empty native MSA directories. Cache
schema 2 uses a separate namespace from earlier file-only entries, which remain
retained. An interrupted publication keeps its partial directory for
diagnosis and cannot be used as a cache hit or silently overwritten.

Worker receipts retain initialization cost, per-request timing, native settings,
random seed policy and every sample. Head restarts reconcile the same attempt
token. Expired workers become `interrupted`; retry is explicit and preserves
earlier attempts. CPU completion is protected by an inherited process lock and
verified immutable artifacts. RF3 completion additionally requires its full
chemistry audit; raw predictions stay separate from the CPU-validated copy.

The head service is `bio-inference.service`, with state under
`/var/lib/bio-inference`. Queue spools use `/mnt/bio-inference-control/spool`,
a dedicated NFS `inference-control` subtree with immediate metadata validation.
Only this mount path may access control artifacts; model and database mounts
retain their normal caches. The exported subtree must exist before first use.
Worker startup verifies the exact source/options on both clients and mounts it
on the worker if needed. Prepared caches and writable job inputs
are under `/mnt/bio-shared/inference`; completed ordinary submissions are copied
to `/var/lib/bio-runs` for the existing local fetch workflow. Protect both the
queue database and shared receipts when backing up or migrating the controller.
