import type { InputFormat, MolecularInput, MoleculeType } from "./types";

type EditorText = {
  id: string;
  name: string;
  text: string;
  molecule_type: MoleculeType;
  format: InputFormat;
};
export interface InputDraft {
  tab: "paste" | "upload" | "library";
  upload_type: MoleculeType;
  paste: EditorText;
  library: EditorText;
}

function emptyText(): EditorText {
  return {
    id: crypto.randomUUID(),
    name: "",
    text: "",
    molecule_type: "protein",
    format: "sequence",
  };
}

export function restoreInputDraft(value: unknown): InputDraft {
  const fresh: InputDraft = {
    tab: "paste",
    upload_type: "protein",
    paste: emptyText(),
    library: emptyText(),
  };
  if (!value || typeof value !== "object") return fresh;
  const draft = value as InputDraft;
  const validType = (value: string) =>
    ["protein", "dna", "rna", "ligand", "assembly", "structure"].includes(
      value,
    );
  if (
    !["paste", "upload", "library"].includes(draft.tab) ||
    !validType(draft.upload_type)
  )
    return fresh;
  const validText = (item: EditorText) =>
    item &&
    typeof item.id === "string" &&
    item.id.length > 0 &&
    typeof item.name === "string" &&
    typeof item.text === "string" &&
    validType(item.molecule_type) &&
    [
      "sequence",
      "fasta",
      "smiles",
      "ccd",
      "sdf",
      "pdb",
      "mmcif",
      "library-json",
      "contigs",
    ].includes(item.format);
  return validText(draft.paste) && validText(draft.library) ? draft : fresh;
}

export function nextChain(inputs: MolecularInput[]): string {
  const used = new Set(inputs.map((input) => input.chain_id));
  for (let i = 0; ; i++) {
    const candidate = i < 26 ? String.fromCharCode(65 + i) : `C${i + 1}`;
    if (!used.has(candidate)) return candidate;
  }
}

export function inputFromDraft(
  draft: InputDraft,
  inputs: MolecularInput[],
): MolecularInput | null {
  if (draft.tab === "upload") return null;
  const content = draft[draft.tab];
  if (!content.text.trim()) return null;
  const format =
    content.format === "sequence" && content.text.trimStart().startsWith(">")
      ? "fasta"
      : content.format;
  return {
    id: content.id,
    name:
      content.name.trim() ||
      (draft.tab === "library"
        ? content.text.trim()
        : `Input ${inputs.length + 1}`),
    molecule_type: content.molecule_type,
    chain_id: nextChain(inputs),
    // Preserve the source exactly; native parsers own chemistry interpretation.
    source:
      draft.tab === "library"
        ? { kind: "library", ref: content.text.trim() }
        : { kind: "text", text: content.text, format },
  };
}

export function clearActiveDraft(draft: InputDraft): InputDraft {
  return draft.tab === "upload"
    ? draft
    : {
        ...draft,
        [draft.tab]: {
          ...draft[draft.tab],
          id: crypto.randomUUID(),
          text: "",
          name: "",
        },
      };
}
