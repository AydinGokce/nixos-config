import { useEffect, useRef, useState } from "react";
import {
  ArrowDownToLine,
  Braces,
  Check,
  FileUp,
  Layers3,
  Link2,
  Plus,
  Trash2,
} from "lucide-react";
import {
  bytesLabel,
  formatForFile,
  inputCount,
  moleculeLabels,
} from "../domain";
import {
  clearActiveDraft,
  inputFromDraft,
  nextChain,
  type InputDraft,
} from "../inputDraft";
import type {
  InputFormat,
  MolecularInput,
  MoleculeType,
  UploadReceipt,
} from "../types";

interface Props {
  inputs: MolecularInput[];
  setInputs: (inputs: MolecularInput[]) => void;
  draft: InputDraft;
  setDraft: (draft: InputDraft) => void;
  mode: "batch" | "assembly";
  setMode: (mode: "batch" | "assembly") => void;
  upload: (
    file: File,
    progress: (value: number) => void,
  ) => Promise<UploadReceipt>;
  connected: boolean;
  importRequest?: number;
  onImportConsumed?: () => void;
}
export function InputBuilder({
  inputs,
  setInputs,
  draft,
  setDraft,
  mode,
  setMode,
  upload,
  connected,
  importRequest,
  onImportConsumed,
}: Props) {
  const { tab } = draft;
  const active = tab === "library" ? "library" : "paste";
  const { text, name, format } = draft[active];
  const type =
    tab === "upload" ? draft.upload_type : draft[active].molecule_type;
  const setTab = (tab: InputDraft["tab"]) => setDraft({ ...draft, tab });
  const setFormat = (format: InputFormat) =>
    setDraft({ ...draft, [active]: { ...draft[active], format } });
  const setText = (text: string) =>
    setDraft({ ...draft, [active]: { ...draft[active], text } });
  const setName = (name: string) =>
    setDraft({ ...draft, [active]: { ...draft[active], name } });
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (importRequest) setTab("upload");
  }, [importRequest]);
  useEffect(() => {
    if (!importRequest || tab !== "upload") return;
    const timer = requestAnimationFrame(() => {
      if (window.bioDesktop?.openImportDialog)
        void window.bioDesktop
          .openImportDialog()
          .catch((e) => setError(`Could not open file picker: ${String(e)}`))
          .finally(() => onImportConsumed?.());
      else {
        fileRef.current?.click();
        onImportConsumed?.();
      }
    });
    return () => cancelAnimationFrame(timer);
  }, [importRequest, tab]);
  const currentInputs = useRef(inputs);
  currentInputs.current = inputs;
  const formats: InputFormat[] =
    type === "ligand"
      ? ["smiles", "ccd", "sdf"]
      : type === "structure"
        ? ["pdb", "mmcif", "contigs"]
        : type === "assembly"
          ? ["library-json"]
          : ["sequence", "fasta"];
  function changeType(next: MoleculeType) {
    if (tab === "upload") {
      setDraft({ ...draft, upload_type: next });
      return;
    }
    setDraft({
      ...draft,
      [active]: {
        ...draft[active],
        molecule_type: next,
        format:
          next === "ligand"
            ? "smiles"
            : next === "structure"
              ? "pdb"
              : next === "assembly"
                ? "library-json"
                : "sequence",
      },
    });
  }
  function addText() {
    setError("");
    const row = inputFromDraft(draft, currentInputs.current);
    if (!row) {
      setError(
        tab === "library"
          ? "Enter a library reference."
          : "Paste your molecular input first.",
      );
      return;
    }
    setInputs([...currentInputs.current, row]);
    setDraft(clearActiveDraft(draft));
  }
  async function addFiles(files: FileList | File[]) {
    setError("");
    if (!connected) {
      setError("Connect to the head before uploading files.");
      return;
    }
    const added: MolecularInput[] = [];
    const problems: string[] = [];
    for (const file of Array.from(files)) {
      const detected = formatForFile(file.name);
      if (!detected) {
        problems.push(
          `${file.name}: unsupported extension; paste the content with an explicit format.`,
        );
        continue;
      }
      try {
        setUploading(file.name);
        setProgress(0);
        const receipt = await upload(file, setProgress);
        added.push({
          id: crypto.randomUUID(),
          name: file.name,
          molecule_type: ["pdb", "mmcif"].includes(detected)
            ? "structure"
            : ["sdf", "smiles"].includes(detected)
              ? "ligand"
              : type,
          chain_id: nextChain([...currentInputs.current, ...added]),
          source: {
            kind: "upload",
            upload_id: receipt.upload_id,
            format: detected,
          },
          bytes: receipt.bytes,
          sha256: receipt.sha256,
        });
      } catch (e) {
        problems.push(
          `${file.name}: ${e instanceof Error ? e.message : String(e)}`,
        );
      }
    }
    const merged = [...currentInputs.current];
    for (const input of added)
      merged.push({ ...input, chain_id: nextChain(merged) });
    setInputs(merged);
    setUploading(null);
    setError(problems.join("\n"));
    if (fileRef.current) fileRef.current.value = "";
  }
  const update = (id: string, patch: Partial<MolecularInput>) =>
    setInputs(inputs.map((i) => (i.id === id ? { ...i, ...patch } : i)));
  return (
    <section className="panel input-panel">
      <div className="section-title">
        <span className="section-number">01</span>
        <div>
          <h2>Molecular inputs</h2>
          <p>Start with sequences, files, or your saved constructs.</p>
        </div>
      </div>
      <div className="mode-switch" aria-label="Input grouping">
        <button
          className={mode === "batch" ? "active" : ""}
          onClick={() => setMode("batch")}
          aria-pressed={mode === "batch"}
        >
          <ArrowDownToLine size={19} />
          <span>
            Separate predictions
            <small>Each input is an independent target</small>
          </span>
          {mode === "batch" && <Check size={16} />}
        </button>
        <button
          className={mode === "assembly" ? "active" : ""}
          onClick={() => setMode("assembly")}
          aria-pressed={mode === "assembly"}
        >
          <Layers3 size={19} />
          <span>
            One assembly<small>Components interact in one complex</small>
          </span>
          {mode === "assembly" && <Check size={16} />}
        </button>
      </div>
      <div className="editor-tabs" role="tablist" aria-label="Add input method">
        {(
          [
            ["paste", Braces, "Paste input"],
            ["upload", FileUp, "Upload files"],
            ["library", Link2, "Library reference"],
          ] as const
        ).map(([id, Icon, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            className={tab === id ? "active" : ""}
            onClick={() => {
              setTab(id);
              setError("");
            }}
          >
            <Icon size={16} />
            {label}
          </button>
        ))}
      </div>
      <div className="input-editor" role="tabpanel">
        <div className="field-row">
          <label>
            Molecule type
            <select
              value={type}
              onChange={(e) => changeType(e.target.value as MoleculeType)}
            >
              {Object.entries(moleculeLabels).map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          {tab === "paste" && (
            <label>
              Input format
              <select
                value={format}
                onChange={(e) => setFormat(e.target.value as InputFormat)}
              >
                {formats.map((f) => (
                  <option key={f} value={f}>
                    {f === "sequence" ? "Plain sequence" : f.toUpperCase()}
                  </option>
                ))}
              </select>
            </label>
          )}
          {tab !== "upload" && (
            <label className="grow">
              Name <span className="muted">(optional)</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={
                  tab === "library"
                    ? "My saved construct"
                    : "e.g. Designed binder 01"
                }
              />
            </label>
          )}
        </div>
        {tab === "upload" ? (
          <div
            className={`dropzone ${dragging ? "dragging" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              if (!uploading) void addFiles(e.dataTransfer.files);
            }}
          >
            <div className="drop-icon">
              <FileUp size={28} />
            </div>
            <strong>
              {uploading ? `Uploading ${uploading}` : "Drop your files here"}
            </strong>
            <span>
              {uploading
                ? `${Math.round(progress)}% · exact source bytes retained`
                : "FASTA, CIF, PDB, SDF, SMILES, or library JSON · multiple files welcome"}
            </span>
            <input
              ref={fileRef}
              type="file"
              multiple
              aria-label="Choose molecular files"
              accept=".fasta,.fa,.faa,.fna,.pdb,.cif,.mmcif,.sdf,.mol,.smi,.smiles,.json"
              onChange={(e) => e.target.files && void addFiles(e.target.files)}
              disabled={!!uploading}
            />
            <button
              className="button secondary"
              disabled={!!uploading}
              onClick={() => fileRef.current?.click()}
            >
              Browse files
            </button>
            {uploading && <progress value={progress} max={100} />}
          </div>
        ) : (
          <>
            <textarea
              aria-label={
                tab === "library" ? "Library reference" : "Molecular input"
              }
              className={`sequence-input ${tab === "library" ? "short" : ""}`}
              spellCheck={false}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={
                tab === "library"
                  ? "construct:my-binder@2\n"
                  : type === "ligand"
                    ? format === "ccd"
                      ? "ATP"
                      : "CC(=O)O"
                    : type === "dna"
                      ? ">promoter\nACGT…"
                      : type === "rna"
                        ? ">guide_rna\nACGU…"
                        : ">binder_01\nEVQLVESGGGLVQPGGSLRLSCAAS…"
              }
            />
            <div className="editor-footer">
              <span>
                {tab === "library"
                  ? "References resolve to exact revisions on the head."
                  : type === "dna" || type === "rna"
                    ? "For synthetic amidites, use an exact library record or supported structured input."
                    : "Original text is preserved. Compatibility is checked before launch."}
              </span>
              <button className="button secondary" onClick={addText}>
                <Plus size={16} />
                Add {mode === "assembly" ? "component" : "input"}
              </button>
            </div>
          </>
        )}
        {error && (
          <div role="alert" className="inline-error">
            {error}
          </div>
        )}
        {tab !== "upload" && text.trim() && (
          <p className="launch-note">
            This {tab === "library" ? "reference" : "input"} is included when
            you check compatibility. Use Add{" "}
            {mode === "assembly" ? "component" : "input"} to enter another.
          </p>
        )}
        {(["paste", "library"] as const)
          .filter((method) => method !== tab && draft[method].text.trim())
          .map((method) => (
            <p className="launch-note" key={method}>
              Your {method === "paste" ? "pasted input" : "library reference"}{" "}
              draft is saved. Return to{" "}
              {method === "paste" ? "Paste input" : "Library reference"} to
              include it.
            </p>
          ))}
      </div>
      {inputs.length > 0 && (
        <div className="input-inventory">
          <div className="inventory-header">
            <strong>
              {inputs.length} {mode === "assembly" ? "components" : "inputs"}{" "}
              added
            </strong>
            <span>Review types before submitting</span>
          </div>
          {inputs.map((input) => (
            <div className="input-row" key={input.id}>
              <span className={`molecule-dot ${input.molecule_type}`} />
              <div className="input-details">
                <input
                  aria-label={`Name for ${input.name}`}
                  value={input.name}
                  onChange={(e) => update(input.id, { name: e.target.value })}
                />
                <small>
                  {input.source.kind === "library"
                    ? input.source.ref
                    : `${input.source.format.toUpperCase()} · ${input.source.kind === "text" ? `${inputCount(input)} record${inputCount(input) === 1 ? "" : "s"}` : bytesLabel(input.bytes)}`}
                </small>
              </div>
              <select
                aria-label={`Type for ${input.name}`}
                value={input.molecule_type}
                onChange={(e) =>
                  update(input.id, {
                    molecule_type: e.target.value as MoleculeType,
                  })
                }
              >
                {Object.entries(moleculeLabels).map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
              </select>
              {mode === "assembly" && (
                <label className="chain-field">
                  Chain
                  <input
                    aria-label={`Chain for ${input.name}`}
                    value={input.chain_id ?? ""}
                    onChange={(e) =>
                      update(input.id, { chain_id: e.target.value })
                    }
                    maxLength={12}
                  />
                </label>
              )}
              <button
                className="icon-button"
                title={`Remove ${input.name}`}
                aria-label={`Remove ${input.name}`}
                onClick={() =>
                  setInputs(inputs.filter((i) => i.id !== input.id))
                }
              >
                <Trash2 size={16} />
              </button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
