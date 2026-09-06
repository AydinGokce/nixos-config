import {
  Check,
  ChevronDown,
  Cpu,
  Info,
  LockKeyhole,
  Sparkles,
} from "lucide-react";
import { useState } from "react";
import { compatibility } from "../domain";
import type { Catalog, MolecularInput, UploadReceipt } from "../types";
interface Props {
  catalog: Catalog;
  inputs: MolecularInput[];
  mode: "batch" | "assembly";
  selected: string[];
  setSelected: (ids: string[]) => void;
  backend: "public" | "private";
  setBackend: (b: "public" | "private") => void;
  execution: "auto" | "resident" | "ephemeral";
  setExecution: (e: "auto" | "resident" | "ephemeral") => void;
  settings: Record<string, Record<string, unknown>>;
  setSettings: (s: Record<string, Record<string, unknown>>) => void;
  upload: (
    file: File,
    progress: (value: number) => void,
  ) => Promise<UploadReceipt>;
}
function LabelsUpload({
  value,
  onChange,
  upload,
}: {
  value: unknown;
  onChange: (id?: string) => void;
  upload: Props["upload"];
}) {
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  async function choose(file?: File) {
    if (!file) return;
    setBusy(true);
    try {
      const receipt = await upload(file, (n) =>
        setStatus(`Uploading ${Math.round(n)}%`),
      );
      onChange(receipt.upload_id);
      setStatus(`${file.name} attached · ${receipt.sha256.slice(0, 12)}…`);
    } catch (e) {
      setStatus(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="labels-upload">
      <input
        type="file"
        aria-label="Activity labels CSV"
        accept=".csv,text/csv"
        disabled={busy}
        onChange={(e) => void choose(e.target.files?.[0])}
      />
      <small>
        {status ||
          (value
            ? "Activity labels attached"
            : "CSV columns: variant,activity. Original bytes are retained.")}
      </small>
      {!!value && (
        <button
          className="text-link"
          onClick={(e) => {
            e.preventDefault();
            onChange();
            setStatus("");
          }}
        >
          Remove labels
        </button>
      )}
    </div>
  );
}
export function ModelPicker({
  catalog,
  inputs,
  mode,
  selected,
  setSelected,
  backend,
  setBackend,
  execution,
  setExecution,
  settings,
  setSettings,
  upload,
}: Props) {
  return (
    <section className="panel model-panel">
      <div className="section-title">
        <span className="section-number">02</span>
        <div>
          <h2>Choose your models</h2>
          <p>Run several models on the same input.</p>
        </div>
      </div>
      <div className="model-list">
        {catalog.models.map((model) => {
          const reason = compatibility(model, inputs, mode);
          const active = selected.includes(model.id);
          return (
            <div
              className={`model-option ${active ? "selected" : ""} ${reason ? "unavailable" : ""}`}
              key={model.id}
            >
              <label>
                <input
                  type="checkbox"
                  checked={active}
                  disabled={!!reason && !active}
                  onChange={() =>
                    setSelected(
                      active
                        ? selected.filter((id) => id !== model.id)
                        : [...selected, model.id],
                    )
                  }
                />
                <span className="model-checkbox">
                  {active && <Check size={14} />}
                </span>
                <div>
                  <div className="model-name">
                    {model.name}
                    <span
                      className={`output-tag ${model.output_kind ?? "structure"}`}
                    >
                      {model.output_kind === "structure" || !model.output_kind
                        ? "3D structure"
                        : model.output_kind}
                    </span>
                  </div>
                  <p>{model.description}</p>
                  {reason && <small className="model-reason">{reason}</small>}
                </div>
              </label>
              {model.limitations?.length ? (
                <details>
                  <summary>
                    <Info size={12} />
                    Input limits
                  </summary>
                  <ul>
                    {model.limitations.map((limit) => (
                      <li key={limit}>{limit}</li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </div>
          );
        })}
      </div>
      {!catalog.models.length && (
        <div className="empty-small">
          <Cpu size={24} />
          <span>Connect to load installed models and their capabilities.</span>
        </div>
      )}
      <div className="search-options">
        <div className="small-heading">
          Sequence search <span>MSA backend</span>
        </div>
        <div className="segmented">
          <button
            className={backend === "private" ? "active" : ""}
            disabled={catalog.msa_backends?.includes("private") === false}
            onClick={() => setBackend("private")}
          >
            <LockKeyhole size={14} />
            Private databases
          </button>
          <button
            className={backend === "public" ? "active" : ""}
            disabled={catalog.msa_backends?.includes("public") === false}
            onClick={() => setBackend("public")}
          >
            Public service
          </button>
        </div>
        <p>
          {backend === "private"
            ? "Uses the configured private search service and records its database provenance."
            : "Sends protein queries to the configured public search service."}
        </p>
      </div>
      <details className="advanced">
        <summary>
          Run settings
          <ChevronDown size={15} />
        </summary>
        <label>
          Execution
          <select
            value={execution}
            onChange={(e) => setExecution(e.target.value as Props["execution"])}
          >
            <option value="auto">
              Automatic · reuse a ready worker when compatible
            </option>
            <option value="resident">Resident worker only</option>
            <option value="ephemeral">Dedicated ephemeral worker</option>
          </select>
        </label>
        {catalog.models
          .filter((m) => selected.includes(m.id))
          .map((model) =>
            Object.entries(model.settings ?? {}).map(([key, spec]) => (
              <label key={`${model.id}/${key}`}>
                {model.name} ·{" "}
                {key === "labels_upload_id"
                  ? "Activity labels (optional)"
                  : (spec.label ?? key)}
                {key === "labels_upload_id" ? (
                  <LabelsUpload
                    value={settings[model.id]?.[key]}
                    upload={upload}
                    onChange={(id) => {
                      const next = { ...settings[model.id] };
                      if (id) next[key] = id;
                      else delete next[key];
                      setSettings({ ...settings, [model.id]: next });
                    }}
                  />
                ) : spec.enum ? (
                  <select
                    aria-label={`${model.name} · ${spec.label ?? key}`}
                    value={String(settings[model.id]?.[key] ?? "")}
                    onChange={(e) => {
                      const next = { ...settings[model.id] };
                      if (e.target.value === "") delete next[key];
                      else
                        next[key] =
                          spec.type === "integer" || spec.type === "number"
                            ? Number(e.target.value)
                            : e.target.value;
                      setSettings({ ...settings, [model.id]: next });
                    }}
                  >
                    <option value="">Native default</option>
                    {spec.enum.map((value) => (
                      <option key={String(value)} value={String(value)}>
                        {String(value)}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    aria-label={`${model.name} · ${spec.label ?? key}`}
                    type={
                      spec.type === "number" || spec.type === "integer"
                        ? "number"
                        : "text"
                    }
                    placeholder={`Native default${spec.default === undefined ? "" : ` · ${String(spec.default)}`}`}
                    value={String(settings[model.id]?.[key] ?? "")}
                    min={spec.minimum}
                    max={spec.maximum}
                    onChange={(e) => {
                      const next = { ...settings[model.id] };
                      if (!e.target.value) delete next[key];
                      else
                        next[key] = ["integer", "number"].includes(spec.type)
                          ? Number(e.target.value)
                          : e.target.value;
                      setSettings({ ...settings, [model.id]: next });
                    }}
                  />
                )}
              </label>
            )),
          )}
        <p>
          <Sparkles size={13} /> Leaving settings empty preserves each model’s
          native defaults.
        </p>
      </details>
    </section>
  );
}
