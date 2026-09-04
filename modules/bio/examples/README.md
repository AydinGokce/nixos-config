# modules/bio example fixtures

Tiny inputs for smoke-testing the `bio-*` tools.

| File | What | Used by |
|------|------|---------|
| `peptide.fasta` | a 77-aa single chain | `bio-esm`, `bio-rfaa`, structure-free tests |
| `evolvepro_variants.fasta` | WT + 5 single mutants | `bio-evolvepro embed` |
| `evolvepro_labels.csv` | measured activity for 3 of them (`variant,activity`) | `bio-evolvepro evolve` |

For a ProteinMPNN / RFdiffusion input backbone, use one of the PDBs bundled in the
ProteinMPNN clone, e.g. `/opt/bio/src/proteinmpnn/inputs/PDB_homooligomers/pdbs/6EHB.pdb`.

## Quick smoke tests

```bash
# ESM-2: per-sequence naturalness score
bio-esm score -i modules/bio/examples/peptide.fasta

# EVOLVEpro: embed variants, then rank the next round to test
bio-evolvepro embed  -i modules/bio/examples/evolvepro_variants.fasta -o /tmp/emb.csv
bio-evolvepro evolve -e /tmp/emb.csv -l modules/bio/examples/evolvepro_labels.csv -n 2 -o /tmp/next.csv

# ProteinMPNN: design sequences for a backbone
bio-mpnn --pdb /opt/bio/src/proteinmpnn/inputs/PDB_homooligomers/pdbs/6EHB.pdb --chains A --num-seqs 4 --out /tmp/mpnn

# RFdiffusion: generate a small monomer backbone, then view it
bio-rfdiffusion --contigs '[60-60]' --num-designs 1 --out /tmp/rfd/mono
bio-viz --render /tmp/rfd/mono_0.pdb -o /tmp/mono.png

# RoseTTAFold-All-Atom: single-sequence, template-free fold
bio-rfaa --fasta modules/bio/examples/peptide.fasta --name pep --out /tmp/rfaa
```

Reference outputs from the initial install smoke test are kept (non-versioned) under
`/opt/bio/runs/smoketest-20260904/`.
