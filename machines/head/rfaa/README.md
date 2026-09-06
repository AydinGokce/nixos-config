# Full cloud RoseTTAFold All-Atom

`bio-submit rfaa --fasta input.fasta` uses sequence databases and structure templates.
`bio-submit rfaa --fasta input.fasta --sub single-seq` explicitly selects the lighter,
template-free mode. The input is one protein FASTA record; local `bio-fold rfaa`
forwards the same `--sub` option.

Full mode follows the searches in the pinned [RFAA source, commit
d69ab3a](https://github.com/baker-laboratory/RoseTTAFold-All-Atom/tree/d69ab3a73f8ede31a4cc005fbc076a341d848469):
iterative UniRef30 HHblits searches, BFD when the UniRef alignment is shallow,
PSIPRED secondary structure, then PDB100 HHsearch templates. A protein can have
few homologs or no significant template hits even after successful full searches.
The job records the MSA depth and completed template search in `preparation.json`.

## Database storage and installation

Databases belong on a dedicated shared volume mounted at `/mnt/bio-databases`,
with `RFAA_DB_DIR=/mnt/bio-databases/rfaa`. The existing 100 GB environment/cache
volume cannot hold them. All workers and the installation node must mount the
same database volume at the same path. Provisioning and attachment are separate
from inference; submitting a protein never starts database downloads.

| Dataset | Archive | Installed prefix beneath `RFAA_DB_DIR` |
| --- | ---: | --- |
| UniRef30 June 2020 | 46.5 GiB | `UniRef30_2020_06/UniRef30_2020_06` |
| BFD CASP14 | 271.6 GiB | `bfd/bfd_metaclust_clu_complete_id30_c90_final_seq.sorted_opt` |
| PDB100 March 2021 | 81.2 GiB | `pdb100_2021Mar03/pdb100_2021Mar03` |

Archive size is not installed size. Plan approximately 2.5 TiB for extracted
databases and at least 3 TiB for serial installation with temporary archives and
headroom. A 3300 GB volume is approximately 3 TiB. Retaining all archives requires
another 399 GiB. Provision the volume only with the intended storage retention
and budget accounted for; it continues to incur storage charges without a GPU.

On the head, inspect the manifest and install explicitly:

```bash
bio-rfaa-databases plan
bio-rfaa-databases install
bio-rfaa-databases validate
```

For a separate Ubuntu download worker, copy this resource directory and use:

```bash
python3 /path/to/rfaa/databases.py install --root /mnt/bio-databases/rfaa
```

The downloader needs Python 3, curl, GNU tar with gzip support, and GNU `du`.
Keep it under a persistent service or terminal session; these downloads and
extractions can take hours. `--only uniref30`, `--only bfd`, or `--only pdb100`
operates on one dataset. Repeat the same command after an interruption: partial
downloads resume, incomplete extraction staging is reused, and installed datasets
are checked before being skipped. Archives are removed only after successful
validation and promotion; `--keep-archives` retains them.

BFD uses Google's public AlphaFold mirror and its published MD5. UniRef30 uses
the upstream GWDG mirror, and PDB100 uses the upstream IPD archive. Exact archive
lengths, gzip integrity on extraction, FFindex/data pairs, offsets, and nonzero
data are checked. Receipts record local archive SHA256 values and installed file
sizes. The SHA256 values for UniRef30/PDB100 are installation receipts rather
than independently published authenticity checksums. `validate` is a fast
structural check, not a full reread/hash of terabytes of extracted data.

Existing copies can be registered with `bio-rfaa-databases adopt`; this validates
their layout and writes receipts but does not establish archive provenance.
Run validation on a worker as well as the installer, since cross-client NFS
visibility problems have previously affected this environment. Placeholder
FFindex files from single-sequence mode are deliberately rejected.

## Tools and running a job

The recipe bootstraps a separate shared CPU-tool environment containing HHsuite
3.3.0, PSIPRED 4.01, csblast 2.2.3, and legacy BLAST 2.2.26. Micromamba 2.5.0 is
pinned and its archive SHA256 is checked. Package channels are conda-forge,
biocore, then bioconda; biocore supplies the PSIPRED version used by upstream
RFAA. The resolved packages are saved in `packages-explicit.txt` in that
environment. This environment is separate from RFAA's Python 3.10/CUDA 11.8
torch environment.

```bash
bio-submit rfaa --fasta target.fasta --name target --timeout 21600
bio-submit rfaa --fasta target.fasta --sub single-seq --name quick-check
```

Full preparation defaults to four CPU threads and a 64 GiB HHsuite memory
limit. The worker environment supports `RFAA_CPU` and `RFAA_MEM_GB` overrides;
use a worker with enough host memory for both preprocessing and the model.
The cloud recipe selects Ampere GPUs for the older torch/DGL stack. `RFAA_TOOLS_ROOT` can
override the CPU environment location; its default is
`/mnt/bio-shared/envs/rfaa-tools-v1`.

SignalP is not included. Its licensed signal-peptide trimming stage is optional
and is also omitted by the upstream Docker workflow. Full MSA and template
searches require no API key or gated database credentials. Inputs retain their
complete submitted sequence; provide an already trimmed FASTA when appropriate.

Preparation runs checked subprocesses before model inference. Each output is
promoted from a temporary file only after its command succeeds, and a query/mode
receipt prevents cached single-sequence files from suppressing full searches.
A failed HHblits, PSIPRED, or HHsearch command fails the job. Successful jobs
retain the alignments, template files, tool logs, and preparation receipt under
`<name>/A/` alongside the predicted structure. Single-sequence mode uses a
separate blank template stub and never modifies an installed database.
The pinned parser also handles completed searches whose hits all have fewer
than ten aligned resolved structure residues. Those hits produce blank template
features while the receipt continues to record the completed full search;
malformed template files still fail inference.

## Verification

```bash
python3 -m unittest discover -s machines/head/rfaa -p 'test_*.py'
bash -n machines/head/recipes/rfaa.sh machines/head/rfaa/tools.sh
```

With an installed and patched RFAA environment, exercise the actual template
parser and feature construction without a GPU:

```bash
RFAA_SOURCE=/mnt/bio-shared/src/rfaa \
  /mnt/bio-shared/envs/rfaa/bin/python -m unittest discover \
  -s machines/head/rfaa -p test_templates_runtime.py
```

The fixture tests cover installation recovery, checksum/structural failures,
full search branching, failed tool propagation, cache reuse, FASTA validation,
and rejecting a mode change over existing alignments. During implementation the
actual CPU packages were also installed and exercised on miniature HHsuite
databases through all UniRef/BFD stages, PSIPRED, and HHsearch, producing real
HHR and ATAB outputs. The resulting features were loaded from read-only database
files with [1UBQ coordinate data](https://files.rcsb.org/download/1UBQ.pdb).
An actual local CUDA single-sequence prediction produced a 20-residue, 301-atom
PDB; the miniature database/template pipeline also completed CUDA inference and
produced a 76-residue, 1228-atom PDB. These checks establish tool and model compatibility; full database
installation and a full cloud prediction must still be verified separately.

Primary references: [upstream RFAA setup and Docker
notes](https://github.com/baker-laboratory/RoseTTAFold-All-Atom/blob/d69ab3a73f8ede31a4cc005fbc076a341d848469/README.md),
[upstream search settings](https://github.com/baker-laboratory/RoseTTAFold-All-Atom/blob/d69ab3a73f8ede31a4cc005fbc076a341d848469/make_msa.sh),
[AlphaFold BFD download script](https://github.com/google-deepmind/alphafold/blob/main/scripts/download_bfd.sh),
and [BFD database provenance](https://bfd.mmseqs.com/).
