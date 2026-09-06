# Public/private preparation and prediction comparison

The production default remains public. A complete private database installation,
successful API calls, deep alignments or high predicted confidence alone do not
establish equal prediction accuracy. Comparisons retain full inputs and all
generated samples; failures remain part of the record.

1. Freeze database/tool receipts and the model environment, checkpoint, input
   sequence, seed, sample count, recycles, diffusion steps and template policy.
   Prepare public and private inputs separately. Keep raw HTTP archives, pipeline
   scripts and native input bundles. The public server's live binary is not
   attested by the pinned private candidate.
2. Compare ordered alignment rows, insertions/deletions, headers and pairing
   keys, template hits, coordinates and masks through `prepared.py compare`.
   Confirm native feature replay, including OpenFold3 template features. A
   mismatch requires investigation; do not discard features to make inputs agree.
3. Run the same model/settings on both bundles. Score every sample against the
   same retained experimental structure. Inspect public/private distributions
   and any target-specific regression, with repeat matched-seed runs when GPU
   numerical variability matters. Confidence scores are supplementary.
4. Review breadth before changing the default: the classical diagnostic panel
   below covers only small single-protein constructs. The frozen recent-release
   panel extends the tested lengths and constructs, but neither panel establishes
   low-homology or unseen-fold performance. Add independently screened targets
   and paired assemblies before claiming those use cases. The current wrappers
   do not yet accept complete assembly inputs. Structures and related proteins
   may occur in training/template data, so these panels assess a pipeline change
   rather than generalization to unseen proteins.

`quality.py references` downloads and hashes the RCSB mmCIF source for six initial
diagnostic cases: 1UBQ, 1CSP, 2LZM, 1TEN, 1AKE and 1PGB, label chain A. FASTAs use
the complete canonical deposited polymer sequence. A deposited domain, fusion or
truncated construct is not necessarily a complete native protein. Do not trim
the submitted construct to its observed coordinates. Experimental mapping uses
`label_seq_id`, so unobserved residues are excluded from scoring without shortening
the submitted sequence. Reference coverage is reported. Adenylate kinase 1AKE
also provides a conformationally sensitive case; an isolated-chain prediction
does not assess its ligand-bound assembly.

```sh
python quality.py references --out /path/to/panel
python quality.py score --case /path/to/panel/1ubq_A.json \
  --predictions /path/to/predictions/*.cif --out /path/to/scores.json
```

The analysis environment needs NumPy and Biopython. Scores report C-alpha RMSD
after a proper rigid rotation (reflection excluded), and C-alpha local distance
agreement at 0.5, 1, 2 and 4 Å thresholds for reference contacts within 15 Å.
Both residue-mean and contact-weighted local scores are retained. These are
C-alpha metrics, not the full all-atom lDDT/stereochemical assessment described
in the [original lDDT paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC3799472/).
No file written by this tool automatically approves parity or changes defaults.

Each prediction must contain the complete submitted sequence with finite
C-alpha coordinates. Multi-protein structures require an explicit chain; all
supplied samples are hashed and included in the aggregate. The score does not
validate side-chain geometry, chirality, ligand placement or interfaces. Such
checks remain necessary when extending the workflow to those tasks. Structural
reference metadata and coordinates come from the [RCSB archive](https://www.rcsb.org/)
and its [documented data model](https://data.rcsb.org/).

Native OpenFold3 CIFs may omit occupancy. When alternate conformers are absent,
the scorer supplies occupancy 1.0 only in memory to satisfy Biopython's parser;
the metric uses the original C-alpha coordinates and retained files stay
byte-for-byte unchanged. Missing occupancy with alternate conformers is rejected.
Each score records this parser policy and the scorer source hash.

## Frozen recent-release diagnostic panel

The full reference audit is retained locally at
`/home/aydin/bio-runs/msa-recent-panel-20260906`. It contains the search request
and complete response, candidate metadata, selection/exclusion records, twelve
experimental CIFs, complete FASTAs, residue mappings, construct labels and an
`audit-manifest.json` binding retained files to their hashes. **No predictions
have been run for this panel.** Reference self-comparison only checks that the
scorer can process each case; it is not model accuracy evidence.

Selection was frozen before any predictions:

1. Query the official [RCSB Search API](https://search.rcsb.org/) for initial
   releases from 2025-01-01 through 2026-08-31, experimental X-ray structures at
   resolution at most 2.5 Å, exactly one deposited protein polymer instance and
   entity, 80–600 residues, and no nonpolymer ligand entities. Water is allowed.
   Request one RCSB representative per 30% sequence-identity cluster. The frozen
   response contains 704 matching entities and all 268 cluster representatives;
   the API's `total_count` and `group_by_count` describe different quantities.
2. Retrieve representative metadata through the official
   [RCSB Data API](https://data.rcsb.org/). Require a standard amino-acid sequence,
   one structural model, all reported biological assemblies to be monomeric,
   one label chain, and at least 95% modeled sequence coverage. These checks
   leave 94 candidates: 19 of length 80–149, 44 of length 150–299, and 31 of
   length 300–600. Rejected candidates and reasons remain in `selection.json`;
   overlapping exclusion reasons are not separate rejected structures.
3. Within each length bin, order candidates by the hexadecimal SHA256 of the
   exact UTF-8 string `msa-recent-panel-v1:` followed by the uppercase entity ID,
   for example `9V62_1`. Select the first four. This balances lengths; it is not
   an estimate weighted to the population of deposited proteins.
4. Download the twelve selected CIFs and verify actual `label_seq_id` mappings,
   complete construct FASTAs, finite C-alpha coordinates, and at least 95%
   observed C-alpha coverage. All twelve passed, so no replacements were made.
   Preserve the complete selection if a later prediction fails; do not replace
   difficult targets or report only successful samples.

All selected targets use label chain A. The notes below come from the retained
experimental records and distinguish domains, fusions, mutations and designs:

| PDB | Initial release | Submitted / observed residues | Construct label |
| --- | --- | ---: | --- |
| [9V62](https://www.rcsb.org/structure/9V62) | 2026-05-06 | 106 / 106 | E. coli CyaY |
| [28OJ](https://www.rcsb.org/structure/28OJ) | 2026-04-08 | 88 / 87 | E. coli YdbL, wild-type construct |
| [9KJ7](https://www.rcsb.org/structure/9KJ7) | 2025-11-19 | 129 / 129 | Hen egg-white lysozyme, a familiar protein |
| [9RXR](https://www.rcsb.org/structure/9RXR) | 2026-01-14 | 109 / 109 | PDZK1 PDZ1 domain fused to a URAT1 terminal peptide |
| [9HB2](https://www.rcsb.org/structure/9HB2) | 2025-10-01 | 291 / 291 | Truncated IdeC protease construct with C94S substitution |
| [9GVF](https://www.rcsb.org/structure/9GVF) | 2025-10-08 | 215 / 211 | Computationally designed TRP_18 F116W |
| [9MYB](https://www.rcsb.org/structure/9MYB) | 2025-04-09 | 257 / 247 | Synthetic retro-aldolase RA95-Shell |
| [9R4F](https://www.rcsb.org/structure/9R4F) | 2025-12-03 | 163 / 163 | T4 lysozyme L99A mutant, apo state |
| [9LVN](https://www.rcsb.org/structure/9LVN) | 2026-01-21 | 502 / 502 | Streptomyces klenkii phospholipase D SkPLD |
| [9D01](https://www.rcsb.org/structure/9D01) | 2025-12-10 | 372 / 369 | Synthetic a-iTHR-201 construct |
| [21XQ](https://www.rcsb.org/structure/21XQ) | 2026-05-13 | 402 / 402 | Bacillus glycinifermentans GH8 endoxylanase |
| [9KER](https://www.rcsb.org/structure/9KER) | 2025-11-12 | 418 / 414 | Human phosphoglycerate kinase 1 |

This is a recent-release diagnostic supplement, not a natural-protein population
sample or a generalization benchmark. Recent deposition does not demonstrate
exclusion from any model's training data; templates and close relatives can be
much older. In particular, 9R4F is from the same T4 lysozyme family as classical
case 2LZM. Sequence clustering applies within the queried recent pool and does
not remove overlap with training data or the classical panel. Engineered and
fusion constructs remain labeled rather than being silently treated as intact
native proteins. The monomeric, ligand-free selection also limits claims about
assemblies, ligand-dependent conformations, flexible regions and low-coverage
experimental structures.

Before prediction, retain a run manifest listing every expected model, backend,
case, seed and sample count. Reconcile scoring inputs against that manifest and
record failed or missing runs; `quality.py score` can verify supplied files but
cannot detect omitted runs by itself. Inspect paired public/private changes per
target and length bin, keeping every generated sample. Investigate apparent
regressions against repeated same-input runs to separate numerical variation
from preparation effects. A favorable mean across twelve targets is not proof
of scientific parity and does not authorize switching the production default.

## Reconcile a retained prediction panel

`report.py` reads each backend's `expected-runs.json`, frozen `quality.py`,
`references/` files and every observed `MODEL/CASE/JOB/job.json`. Each completed
job supplies the scorer's `accuracy.json`, `runtime-audit.json`, the complete
`prepared-native/` tree and its effective inference settings. Use the same
reference files for both backends, preserving the original filenames and hashes:

```sh
python report.py --public /path/to/public-runs --out public-progress.json
python report.py --public /path/to/public-runs --private /path/to/private-runs \
  --out comparison.json
```

This uses the standard library and `prepared.py`. It checks actual frozen FASTA,
experimental CIF and residue-mapping hashes, coverage, native materialized files
and rewritten input/runtime documents, exact seed/sample identities, and every
prediction's score binding. OpenFold3 contributes its native `model_config.json`
and `experiment_config.json`. Protenix and Boltz contribute `resolved-settings.json`
from `settings.py`, bound to the runtime audit and source/helper hashes. That
helper reconstructs native resolved options from recorded process arguments in
the same installed package environment; Protenix additionally requires the same
GPU type and applies its native token-count adjustment. It does not load weights
or query an MSA service. This is a settings reconstruction, not a snapshot of
internal inference tensors. The full model/checkpoint/GPU/package audit remains
separate from CPU preparation provenance.

Pairing requires equal frozen scientific settings, references, measured runtime,
effective configurations and scorer versions within a model. Only the original
job path and manifest-bound preparation locations are canonicalized in effective
configurations; scientific values remain intact. Both backend labels must agree
with their actual preparation manifests. Field-level incompatibilities remain
visible. Every complete retry contributes its sample mean, then each compatible
case receives equal weight; no best sample or best retry is selected. The report
lists failed and missing runs, incomplete samples, saved cleanup evidence, and
the complete-pair denominator. Its means describe that subset; missing cases can
bias the result. Failed runs with partial unscored structures are explicitly
counted and need separate investigation.

Directory discovery cannot detect a wholly missing or deleted retry if another
attempt survives. The report therefore labels attempt coverage as unverified;
an immutable prelaunch attempt ledger would be needed for a stronger claim.
Nothing in the report approves parity or changes the public default.
