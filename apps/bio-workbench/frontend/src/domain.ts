import type {
  AnnotationDocument,
  Artifact,
  InputFormat,
  ModelSpec,
  MolecularInput,
  MoleculeType,
  RunStatus,
} from "./types";

export const moleculeLabels: Record<MoleculeType, string> = {
  protein: "Protein",
  dna: "DNA",
  rna: "RNA",
  ligand: "Small molecule",
  assembly: "Library assembly",
  structure: "Structure",
};
export const terminal = (status: RunStatus) =>
  ["succeeded", "partial", "failed", "cancelled"].includes(status);
export const statusLabels: Record<RunStatus, string> = {
  validated: "Ready to submit",
  validation_failed: "Input validation failed",
  created: "Created",
  preparing: "Preparing input",
  queued: "Queued",
  running: "Running",
  validating: "Validating",
  succeeded: "Complete",
  partial: "Partially complete",
  failed: "Failed",
  cancelling: "Cancelling",
  cancelled: "Cancelled",
};
export function formatForFile(name: string): InputFormat | null {
  const suffix = name.toLowerCase().split(".").pop();
  return (
    (
      {
        fasta: "fasta",
        fa: "fasta",
        faa: "fasta",
        fna: "fasta",
        sdf: "sdf",
        mol: "sdf",
        pdb: "pdb",
        cif: "mmcif",
        mmcif: "mmcif",
        smi: "smiles",
        smiles: "smiles",
        json: "library-json",
      } as Record<string, InputFormat>
    )[suffix ?? ""] ?? null
  );
}
export function inputCount(input: MolecularInput): number {
  if (input.source.kind === "text" && input.source.format === "fasta")
    return Math.max(1, (input.source.text.match(/^>/gm) ?? []).length);
  return 1;
}
export function compatibility(
  model: ModelSpec,
  inputs: MolecularInput[],
  mode: "batch" | "assembly",
): string | null {
  if (!model.enabled)
    return model.disabled_reason ?? "Unavailable on this installation";
  if (model.modes && !model.modes.includes(mode))
    return `Does not accept ${mode === "assembly" ? "assemblies" : "batch inputs"}`;
  // Independent batch inputs can use different workflows. Keep a model
  // selectable when it accepts at least one target; the durable native preview
  // must still retain and explain every rejected input/model pair.
  if (
    mode === "batch" &&
    inputs.some((input) => model.molecule_types.includes(input.molecule_type))
  )
    return null;
  const unsupported = [
    ...new Set(
      inputs
        .map((i) => i.molecule_type)
        .filter((t) => !model.molecule_types.includes(t)),
    ),
  ];
  return unsupported.length
    ? `Does not accept ${unsupported.map((t) => moleculeLabels[t]).join(", ")}`
    : null;
}
export function validateDraft(
  inputs: MolecularInput[],
  mode: "batch" | "assembly",
  modelIds: string[],
  models: ModelSpec[],
): string[] {
  const errors: string[] = [];
  if (!inputs.length) errors.push("Add at least one molecular input.");
  if (!modelIds.length) errors.push("Select at least one model.");
  for (const input of inputs) {
    if (!input.name.trim()) errors.push("Every input needs a name.");
    if (input.source.kind === "text" && !input.source.text.trim())
      errors.push(`${input.name}: input is empty.`);
    if (
      input.source.kind === "library" &&
      !/^(construct|assembly):[^\s]+/.test(input.source.ref)
    )
      errors.push(
        `${input.name}: use a construct: or assembly: library reference.`,
      );
    if (mode === "assembly" && inputCount(input) > 1)
      errors.push(
        `${input.name}: split FASTA records into individual components before building an assembly.`,
      );
    if (mode === "assembly" && !/^[A-Za-z0-9]+$/.test(input.chain_id ?? ""))
      errors.push(`${input.name}: supply a chain ID with letters or numbers.`);
  }
  if (
    mode === "assembly" &&
    new Set(inputs.map((i) => i.chain_id)).size !== inputs.length
  )
    errors.push("Assembly chain IDs must be unique.");
  for (const id of modelIds) {
    const model = models.find((m) => m.id === id);
    const reason = model && compatibility(model, inputs, mode);
    if (!model || reason)
      errors.push(
        `${model?.name ?? id}: ${reason ?? "model is no longer available"}.`,
      );
  }
  return errors;
}
export function parseAnnotations(
  value: unknown,
  hash: string,
): AnnotationDocument {
  const doc = value as AnnotationDocument;
  if (
    !doc ||
    doc.schema !== 1 ||
    doc.kind !== "bio-workbench-annotations" ||
    doc.artifact_sha256 !== hash ||
    !Array.isArray(doc.annotations) ||
    doc.annotations.length > 1000
  )
    throw new Error("Annotations do not match this exact structure artifact.");
  const ids = new Set<string>();
  for (const a of doc.annotations) {
    if (
      !a ||
      a.artifact_sha256 !== hash ||
      typeof a.id !== "string" ||
      ids.has(a.id) ||
      typeof a.label !== "string" ||
      a.label.length > 160 ||
      typeof a.note !== "string" ||
      a.note.length > 10000 ||
      !/^#[0-9a-fA-F]{6}$/.test(a.color) ||
      !a.selection ||
      typeof a.selection.chain !== "string" ||
      !Number.isInteger(a.selection.resi) ||
      (a.selection.icode !== undefined && typeof a.selection.icode !== "string")
    )
      throw new Error("Annotation file has invalid or duplicate entries.");
    ids.add(a.id);
  }
  if (
    doc.pending_deletions !== undefined &&
    (!Array.isArray(doc.pending_deletions) ||
      doc.pending_deletions.length > 1000 ||
      doc.pending_deletions.some((id) => typeof id !== "string"))
  )
    throw new Error("Invalid pending annotation deletions.");
  if (doc.local_conflict_copies !== undefined) {
    if (
      !Array.isArray(doc.local_conflict_copies) ||
      doc.local_conflict_copies.length > 100
    )
      throw new Error("Invalid local conflict copies.");
    for (const copy of doc.local_conflict_copies) {
      if (!copy || typeof copy.captured_at !== "string")
        throw new Error("Invalid local conflict copy.");
      parseAnnotations(
        {
          schema: 1,
          kind: doc.kind,
          artifact_sha256: hash,
          annotations: copy.annotations,
          pending_deletions: copy.pending_deletions,
        },
        hash,
      );
    }
  }
  return doc;
}
export function confidenceAvailable(artifact: Artifact): boolean {
  return (
    artifact.confidence?.kind?.toLowerCase() === "plddt" &&
    artifact.confidence.atom_property === "b"
  );
}
export function safeArtifactUrl(url: string): string {
  const parsed = new URL(url, window.location.origin);
  if (
    parsed.origin !== window.location.origin ||
    !/^\/(api\/v1\/|fixtures\/)/.test(parsed.pathname) ||
    parsed.username ||
    parsed.password
  )
    throw new Error("The artifact URL is outside this workbench.");
  return parsed.href;
}
export function bytesLabel(bytes?: number): string {
  return bytes === undefined
    ? ""
    : bytes < 1024
      ? `${bytes} B`
      : bytes < 1024 ** 2
        ? `${(bytes / 1024).toFixed(1)} KB`
        : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}
export function downloadJson(value: unknown, name: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2) + "\n"], {
      type: "application/json",
    }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
