import type { Catalog, Run } from "./types";
// Public experimental structure, not a model prediction. Demo submission is disabled.
export const exampleCatalog: Catalog = {
  msa_backends: ["public", "private"],
  models: [
    {
      id: "rf3",
      name: "RoseTTAFold3",
      description: "Joint protein, nucleic acid, and ligand prediction.",
      enabled: true,
      molecule_types: ["protein", "dna", "rna", "ligand", "assembly"],
      output_kind: "structure",
    },
    {
      id: "boltz2",
      name: "Boltz-2",
      description:
        "Biomolecular structure and supported binding affinity inputs.",
      enabled: true,
      molecule_types: ["protein", "dna", "rna", "ligand", "assembly"],
      output_kind: "structure",
    },
    {
      id: "protenix",
      name: "Protenix",
      description: "All-atom prediction with model-specific chemistry checks.",
      enabled: true,
      molecule_types: ["protein", "dna", "rna", "ligand", "assembly"],
      output_kind: "structure",
    },
    {
      id: "openfold3",
      name: "OpenFold3",
      description: "Protein complexes and supported molecular partners.",
      enabled: true,
      molecule_types: ["protein", "dna", "rna", "ligand", "assembly"],
      output_kind: "structure",
    },
  ],
};
export function exampleRun(hash: string): Run {
  return {
    id: "local-reference-example",
    name: "Ubiquitin · local viewer example",
    mode: "batch",
    status: "succeeded",
    created_at: "1987-01-02T00:00:00Z",
    jobs: [
      {
        id: "reference-only",
        model: "Experimental reference",
        name: "1UBQ — no prediction was run",
        status: "succeeded",
        phase: "Local reference file",
        message:
          "Two views of the same public experimental structure, for exploring viewer controls.",
        artifacts: [0, 1].map((i) => ({
          id: `reference-${i}`,
          name: `1UBQ · view ${i + 1}`,
          kind: "structure" as const,
          format: "mmcif",
          url: "/fixtures/1ubq.cif",
          sha256: hash,
          model: "1UBQ reference",
          provenance: {
            source: "https://www.rcsb.org/structure/1UBQ",
            kind: "experimental-reference",
            note: "Local viewer fixture, not a prediction or an accuracy comparison. Both views contain identical source bytes.",
          },
        })),
      },
    ],
  };
}
