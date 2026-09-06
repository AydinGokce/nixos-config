import { describe, expect, test } from "vitest";
import {
  compatibility,
  confidenceAvailable,
  formatForFile,
  inputCount,
  parseAnnotations,
  validateDraft,
} from "../src/domain";
import { mapArtifact, mapBatch, WorkbenchApi } from "../src/api";
import type { Artifact, ModelSpec, MolecularInput } from "../src/types";
import { parseTable } from "../src/components/ArtifactPreview";
const hash = "a".repeat(64);
const protein: MolecularInput = {
  id: "p",
  name: "binder",
  molecule_type: "protein",
  chain_id: "A",
  source: { kind: "text", format: "fasta", text: ">A\nACDE\n>B\nFGHI\n" },
};
const model: ModelSpec = {
  id: "rf3",
  name: "RF3",
  enabled: true,
  description: "",
  molecule_types: ["protein", "ligand"],
  modes: ["batch", "assembly"],
};
describe("input intent and chemistry boundaries", () => {
  test("multi-record batch stays one exact source until the server expands it", () => {
    expect(inputCount(protein)).toBe(2);
    expect(validateDraft([protein], "batch", ["rf3"], [model])).toEqual([]);
    expect(protein.source).toEqual({
      kind: "text",
      format: "fasta",
      text: ">A\nACDE\n>B\nFGHI\n",
    });
  });
  test("assembly requires separate components and unique explicit chain IDs", () => {
    expect(
      validateDraft([protein], "assembly", ["rf3"], [model]).join(),
    ).toContain("split FASTA");
    const one = {
      ...protein,
      source: {
        kind: "text" as const,
        format: "sequence" as const,
        text: "ACDE",
      },
    };
    expect(
      validateDraft(
        [one, { ...one, id: "other" }],
        "assembly",
        ["rf3"],
        [model],
      ).join(),
    ).toContain("unique");
  });
  test("unsupported modality and disabled parked model remain explicit", () => {
    expect(
      compatibility(model, [{ ...protein, molecule_type: "dna" }], "batch"),
    ).toContain("DNA");
    expect(
      compatibility(
        { ...model, enabled: false, disabled_reason: "RFAA is parked" },
        [],
        "batch",
      ),
    ).toBe("RFAA is parked");
  });
  test("file type does not guess DNA versus protein from the alphabet", () => {
    expect(formatForFile("guide.FASTA")).toBe("fasta");
    expect(formatForFile("modified.sdf")).toBe("sdf");
    expect(formatForFile("script.exe")).toBeNull();
  });
  test("mixed-modality batch allows workflows with a compatible subset but assembly requires every component", () => {
    const proteinOnly = { ...model, molecule_types: ["protein"] };
    const mixed: MolecularInput[] = [
      protein,
      { ...protein, id: "dna", molecule_type: "dna" },
    ];
    expect(compatibility(proteinOnly, mixed, "batch")).toBeNull();
    expect(compatibility(proteinOnly, mixed, "assembly")).toContain("DNA");
  });
});
describe("artifact identity and annotations", () => {
  const annotation = {
    id: "note",
    artifact_sha256: hash,
    label: "Binding site",
    note: "Hypothesis only",
    color: "#aabbcc",
    selection: { chain: "A", resi: 12, icode: "B" },
    created_at: "2026-09-06T00:00:00Z",
  };
  const doc = {
    schema: 1,
    kind: "bio-workbench-annotations",
    artifact_sha256: hash,
    annotations: [annotation],
  };
  test("exact structure hash, insertion code and note survive roundtrip", () => {
    expect(
      parseAnnotations(JSON.parse(JSON.stringify(doc)), hash).annotations[0]
        .selection.icode,
    ).toBe("B");
    expect(() => parseAnnotations(doc, "b".repeat(64))).toThrow(
      "exact structure",
    );
  });
  test("duplicates and malformed residue IDs cannot change a selection", () => {
    expect(() =>
      parseAnnotations({ ...doc, annotations: [annotation, annotation] }, hash),
    ).toThrow("duplicate");
    expect(() =>
      parseAnnotations(
        {
          ...doc,
          annotations: [
            { ...annotation, selection: { chain: "A", resi: NaN } },
          ],
        },
        hash,
      ),
    ).toThrow("invalid");
  });
  test("B factors are never assumed to mean pLDDT", () => {
    const a = {
      confidence: {
        kind: "B-factor",
        atom_property: "b",
        source: "experimental",
      },
    } as Artifact;
    expect(confidenceAvailable(a)).toBe(false);
    expect(
      confidenceAvailable({
        ...a,
        confidence: { kind: "pLDDT", source: "native", atom_property: "b" },
      }),
    ).toBe(true);
    expect(
      confidenceAvailable({
        ...a,
        confidence: { kind: "pLDDT", source: "summary" },
      }),
    ).toBe(false);
  });
  test("failed and interrupted native output stays available without manufactured confidence", () => {
    const a = mapArtifact({
      artifact_id: "a",
      name: "raw.cif",
      sha256: hash,
      role: "structure",
      format: "mmcif",
      size: 3,
      confidence: null,
      qa: null,
    });
    expect(a.confidence).toBeUndefined();
    const b = mapBatch({
      batch_id: "b",
      state: "partial",
      name: "mixed",
      mode: "assembly",
      jobs: [
        {
          job_id: "j",
          state: "interrupted",
          artifacts: [{ artifact_id: "a", role: "structure", format: "mmcif" }],
        },
      ],
    });
    expect(b.jobs[0].status).toBe("failed");
    expect(b.jobs[0].artifacts).toHaveLength(1);
  });
});
test("RPC pagination retains artifact 101 and all history pages", async () => {
  const api = new WorkbenchApi();
  const calls: string[] = [];
  api.rpc = async (method: string, params: Record<string, any> = {}) => {
    calls.push(method);
    if (method === "batch.list")
      return {
        batches: [
          {
            batch_id: params.cursor ? "b2" : "b1",
            state: "complete",
            name: "x",
            jobs: [],
          },
        ],
        next_cursor: params.cursor ? null : "next",
      } as any;
    if (method === "batch.get")
      return {
        batch_id: "b",
        state: "complete",
        jobs: [
          {
            job_id: "j",
            state: "complete",
            artifact_count: 101,
            artifacts: [],
          },
        ],
      } as any;
    if (method === "job.artifacts")
      return {
        artifacts: Array.from({ length: params.cursor ? 1 : 100 }, (_, i) => ({
          artifact_id: `a${params.cursor ? 100 : i}`,
          format: "json",
          role: "data",
        })),
        next_cursor: params.cursor ? null : "last",
      } as any;
    throw new Error(method);
  };
  expect((await api.runs()).map((r) => r.id)).toEqual(["b1", "b2"]);
  expect((await api.run("b")).jobs[0].artifacts).toHaveLength(101);
  expect(calls.filter((c) => c === "job.artifacts")).toHaveLength(2);
});
test("nonstructure CSV preview preserves quoted commas, embedded newline and escaped quotes", () => {
  expect(
    parseTable(
      'variant,activity,note\r\nA12G,0.8,"binding, measured"\r\nV14L,0.3,"line1\nline2 ""quoted"""\n',
    ),
  ).toEqual([
    ["variant", "activity", "note"],
    ["A12G", "0.8", "binding, measured"],
    ["V14L", "0.3", 'line1\nline2 "quoted"'],
  ]);
});
