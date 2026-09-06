# EVOLVEpro-style cloud jobs

The cloud recipe runs the same two-stage implementation as `bio-evolvepro`:
ESM-2 mean embeddings of all variants, then a random forest or XGBoost regressor
trained on measured activities. It ranks unmeasured candidates by predicted
activity, with larger activity values preferred. This is the toolkit's compact
implementation of the [EVOLVEpro approach](https://github.com/mat10d/EvolvePro),
using ESM-2; it does not reproduce all upstream models or experimental workflows.

From the workstation:

```bash
bio-fold evolvepro \
  --fasta modules/bio/examples/evolvepro_variants.fasta \
  --labels modules/bio/examples/evolvepro_labels.csv \
  --num 2 --out ~/bio-runs/evolvepro-demo
```

On the head, use `bio-submit evolvepro` with the same input flags. The FASTA must
include both measured and candidate variants, with unique identifiers after
`>`. Labels use exactly those identifiers:

```csv
variant,activity
wt,1.0
m1,0.2
m2,1.8
```

The default embedding model is `esm2_t33_650M_UR50D`; choose another ESM-2 model
with `--model`. `--num` defaults to 12 and is capped by the number of candidates.
Regression options follow `--`:

```bash
bio-fold evolvepro --fasta variants.fasta --labels measured.csv --num 12 \
  --model esm2_t33_650M_UR50D -- --regressor xgb --seed 37
```

Without `--labels`, the job proposes an initial diverse set using k-means.
Identical embeddings share a representative, so the set can be smaller than
`--num`. `--sub embed` runs only the embedding stage and skips the regression
environment. For a quick integration check, use the small
`--model esm2_t6_8M_UR50D`; success with this model validates the workflow, not the
quality of activity predictions.

Results are fetched through the normal `bio-submit` / `bio-fold` flow:

| File | Contents |
| --- | --- |
| `embeddings.csv` | One embedding row per input variant; reusable with local `bio-evolvepro evolve` |
| `next_round.csv` | Up to `--num` selected unmeasured variants with predicted activities |
| `ranking.csv` | All unmeasured variants sorted by prediction; without labels, the diverse selected set |
| `selected.fasta` | Selected sequences in the same order as `next_round.csv` |
| `variants.fasta`, `labels.csv` | Normalized input copies (`labels.csv` only when supplied) |
| `run.json` | Model/settings, input hashes, counts, selected IDs, completion state, timestamps |

An embedding-only job writes `embeddings.csv`, `variants.fasta`, and `run.json`.
These jobs produce sequences and tables. Structure visualization is available
after folding a selected sequence with one of the structure predictors.

The recipe caches the two Python environments under
`/mnt/bio-shared/envs/evolvepro-{plm,core}` and public ESM-2 weights under the
shared Hugging Face cache. No extra API key or sequence database is needed.
Variant IDs, empty/invalid sequences, missing label IDs, duplicate measurements,
and nonfinite activities are checked before installing dependencies. The head
can also validate inputs before launching a GPU:

```bash
python3 /etc/bio-tools/py/evolvepro_cloud.py --validate-only \
  --input variants.fasta --labels measured.csv --num 12
```

Development checks:

```bash
python3 -m unittest discover -s modules/bio/tests -p 'test_evolvepro_cloud.py'
# Also exercise real regression if its environment is already installed:
EVOLVEPRO_CORE_PYTHON=/opt/bio/envs/evolvepro-core/bin/python \
  python3 -m unittest discover -s modules/bio/tests -p 'test_evolvepro_cloud.py'
```

On NixOS, use the runtime library paths from `/etc/bio/config.sh` when calling a
venv Python directly, as the installed `bio-evolvepro` wrapper already does.
