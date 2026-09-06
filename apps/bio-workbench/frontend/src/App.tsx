import { readLocal, writeLocal } from "./storage";
import {
  Component,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Activity,
  ArrowRight,
  Boxes,
  ChevronRight,
  Columns2,
  FlaskConical,
  LoaderCircle,
  Plug,
  RefreshCw,
  Settings2,
  ShieldCheck,
  X,
} from "lucide-react";
import { WorkbenchApi, type Connection } from "./api";
import { InputBuilder } from "./components/InputBuilder";
import { ModelPicker } from "./components/ModelPicker";
import { RunsPanel } from "./components/RunsPanel";
import { ComparePanel } from "./components/ComparePanel";
import { ConnectionDialog } from "./components/ConnectionDialog";
import { inputCount, terminal, validateDraft } from "./domain";
import {
  clearActiveDraft,
  inputFromDraft,
  restoreInputDraft,
} from "./inputDraft";
import { exampleCatalog, exampleRun } from "./fixtures";
import type {
  Artifact,
  Catalog,
  CreateRun,
  MolecularInput,
  Run,
} from "./types";

const api = new WorkbenchApi();
const draftKey = "bio-workbench.input-draft.v1";
function getDraft(): {
  inputs?: MolecularInput[];
  name?: string;
  mode?: "batch" | "assembly";
  editor?: unknown;
} {
  try {
    const raw = readLocal(draftKey);
    const d = raw && JSON.parse(raw);
    return d && Array.isArray(d.inputs) && d.inputs.length <= 128 ? d : {};
  } catch {
    return {};
  }
}
export class AppBoundary extends Component<
  { children: ReactNode },
  { error: string }
> {
  state = { error: "" };
  static getDerivedStateFromError(error: Error) {
    return { error: error.message };
  }
  render() {
    return this.state.error ? (
      <main className="fatal-error">
        <FlaskConical size={40} />
        <h1>The workbench could not display this view.</h1>
        <p>{this.state.error}</p>
        <button className="button primary" onClick={() => location.reload()}>
          Reload app
        </button>
        <p>Submitted jobs remain on the head.</p>
      </main>
    ) : (
      this.props.children
    );
  }
}
export default function App() {
  const [importRequest, setImportRequest] = useState(0);
  const [tab, setTab] = useState<"prepare" | "runs" | "compare">("prepare");
  const [catalog, setCatalog] = useState<Catalog>({ models: [] });
  const [runs, setRuns] = useState<Run[]>([]);
  const [connection, setConnection] = useState<Connection>();
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [demo, setDemo] = useState(false);
  const initial = useRef(getDraft());
  const [inputs, setInputs] = useState<MolecularInput[]>(
    initial.current.inputs ?? [],
  );
  const [editor, setEditor] = useState(() =>
    restoreInputDraft(initial.current.editor),
  );
  const pendingInput = inputFromDraft(editor, inputs);
  const effectiveInputs = pendingInput ? [...inputs, pendingInput] : inputs;
  const [name, setName] = useState(initial.current.name ?? "");
  const [mode, setMode] = useState<"batch" | "assembly">(
    initial.current.mode === "assembly" ? "assembly" : "batch",
  );
  const [models, setModels] = useState<string[]>([]);
  const [backend, setBackend] = useState<"public" | "private">("public");
  const [execution, setExecution] = useState<"auto" | "resident" | "ephemeral">(
    "auto",
  );
  const [settings, setSettings] = useState<
    Record<string, Record<string, unknown>>
  >({});
  const [selectedRun, setSelectedRun] = useState<string | null>(
    new URLSearchParams(location.search).get("batch"),
  );
  const [selectedStructures, setSelectedStructures] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [draftSaved, setDraftSaved] = useState(true);
  const previewIntent = useRef<
    { signature: string; request: CreateRun } | undefined
  >(undefined);
  const commitIntents = useRef(
    new Map<string, { signature: string; key: string }>(),
  );
  const mergeRun = useCallback(
    (run: Run) =>
      setRuns((current) =>
        [run, ...current.filter((r) => r.id !== run.id)].sort((a, b) =>
          b.created_at.localeCompare(a.created_at),
        ),
      ),
    [],
  );
  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const session = await api.session();
      setConnection(session.connection);
      if (!session.connection.configured) {
        setConnected(false);
        return;
      }
      const [c, r] = await Promise.all([api.catalog(), api.runs()]);
      setCatalog(c);
      setRuns(r);
      setSelectedRun((old) => old ?? r[0]?.id ?? null);
      setConnected(true);
      setDemo(false);
    } catch (e) {
      setConnected(false);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => {
    try {
      writeLocal(draftKey, JSON.stringify({ inputs, name, mode, editor }));
      setDraftSaved(true);
    } catch {
      setDraftSaved(false);
    }
  }, [inputs, name, mode, editor]);
  useEffect(() => {
    if (!connected || demo) return;
    let disposed = false;
    let running = false;
    const tick = async () => {
      if (running) return;
      running = true;
      try {
        const list = await api.runs();
        if (!disposed)
          setRuns((previous) =>
            list.map((r) => ({
              ...r,
              jobs: r.jobs.length
                ? r.jobs
                : (previous.find((old) => old.id === r.id)?.jobs ?? []),
            })),
          );
        if (selectedRun) {
          const r = await api.run(selectedRun);
          if (!disposed) mergeRun(r);
        }
      } catch (e) {
        if (!disposed)
          setError(
            `Live update paused: ${e instanceof Error ? e.message : String(e)}. Jobs remain on the head.`,
          );
      } finally {
        running = false;
      }
    };
    void tick();
    const timer = setInterval(() => void tick(), 4000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [connected, demo, selectedRun, mergeRun]);
  useEffect(() => {
    const open = (id: string) => {
      setSelectedRun(id);
      setTab("runs");
    };
    const unlisten = window.bioDesktop?.onOpenBatch?.(open);
    const event = () => {
      setTab("prepare");
      setImportRequest((v) => v + 1);
    };
    window.addEventListener("bio:import-files", event);
    if (selectedRun) setTab("runs");
    return () => {
      unlisten?.();
      window.removeEventListener("bio:import-files", event);
    };
  }, []);
  const allArtifacts = runs.flatMap((r) =>
    r.jobs.flatMap((j) => j.artifacts ?? []),
  );
  async function preview() {
    setError("");
    const problems = validateDraft(
      effectiveInputs,
      mode,
      models,
      catalog.models,
    );
    if (problems.length) {
      setError(problems.join("\n"));
      return;
    }
    // Commit the visible editor once before networking. A failed preview keeps
    // the exact row IDs and request signature available for an idempotent retry.
    if (pendingInput) {
      setInputs(effectiveInputs);
      setEditor(clearActiveDraft(editor));
    }
    const payload = {
      name:
        name.trim() || `Untitled ${mode === "assembly" ? "assembly" : "batch"}`,
      mode,
      inputs: effectiveInputs,
      models,
      msa_backend: backend,
      execution,
      settings: Object.fromEntries(
        models.map((id) => [id, settings[id] ?? {}]),
      ),
    };
    const signature = JSON.stringify(payload);
    if (previewIntent.current?.signature !== signature)
      previewIntent.current = {
        signature,
        request: { ...payload, request_key: crypto.randomUUID() },
      };
    setBusy(true);
    try {
      const run = await api.preview(previewIntent.current.request);
      mergeRun(run);
      setSelectedRun(run.id);
      setTab("runs");
    } catch (e) {
      setError(
        `${e instanceof Error ? e.message : String(e)}\nIf the connection was interrupted, checking again with unchanged inputs reuses the same preview request.`,
      );
    } finally {
      setBusy(false);
    }
  }
  async function submit(id: string, pairIds: string[]) {
    setError("");
    setBusy(true);
    const signature = JSON.stringify([...pairIds].sort());
    const previous = commitIntents.current.get(id);
    if (!previous || previous.signature !== signature)
      commitIntents.current.set(id, { signature, key: crypto.randomUUID() });
    try {
      mergeRun(
        await api.submit(id, commitIntents.current.get(id)!.key, pairIds),
      );
    } catch (e) {
      setError(
        `${e instanceof Error ? e.message : String(e)}\nSubmission status is uncertain if the connection failed. Retry the same selection to reconcile this request; it will not create duplicate jobs.`,
      );
    } finally {
      setBusy(false);
    }
  }
  const cancel = async (id: string) => {
    try {
      mergeRun(await api.cancel(id));
    } catch (e) {
      setError(
        `Cancellation was not confirmed: ${e instanceof Error ? e.message : String(e)}. Check the run status before trying again.`,
      );
    }
  };
  const compare = (artifact: Artifact) => {
    setSelectedStructures((previous) =>
      previous.includes(artifact.id)
        ? previous
        : [...previous.slice(-1), artifact.id],
    );
    setTab("compare");
  };
  async function showExample() {
    const data = await (await fetch("/fixtures/1ubq.cif")).arrayBuffer();
    const hash = Array.from(
      new Uint8Array(await crypto.subtle.digest("SHA-256", data)),
      (b) => b.toString(16).padStart(2, "0"),
    ).join("");
    const run = exampleRun(hash);
    setDemo(true);
    setCatalog(exampleCatalog);
    setRuns([run]);
    setSelectedStructures(run.jobs[0].artifacts!.map((a) => a.id));
    setTab("compare");
    setError("");
  }
  const activeCount = runs.filter(
    (r) =>
      !terminal(r.status) &&
      !["validated", "validation_failed"].includes(r.status),
  ).length;
  const predictedCount =
    (mode === "assembly"
      ? 1
      : effectiveInputs.reduce((n, input) => n + inputCount(input), 0)) *
    models.length;
  return (
    <div className="app-shell">
      <aside className="app-rail">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setTab("prepare");
          }}
          aria-label="Bio Workbench home"
        >
          <FlaskConical size={25} />
        </a>
        <div className="rail-divider" />
        {(
          [
            ["prepare", Boxes, "Prepare inputs"],
            ["runs", Activity, "Run history"],
            ["compare", Columns2, "Compare structures"],
          ] as const
        ).map(([id, Icon, label]) => (
          <button
            key={id}
            className={`rail-button ${tab === id ? "active" : ""}`}
            aria-label={label}
            title={label}
            aria-current={tab === id ? "page" : undefined}
            onClick={() => setTab(id)}
          >
            <Icon size={21} />
            {id === "runs" && activeCount > 0 && (
              <span className="rail-count">{activeCount}</span>
            )}
          </button>
        ))}
        <div className="rail-bottom">
          <button
            className="rail-button"
            aria-label="Connection settings"
            title="Connection settings"
            onClick={() => setConnectionOpen(true)}
          >
            <Settings2 size={20} />
          </button>
          <span className="version-mark">BIO</span>
        </div>
      </aside>
      <div className="app-main">
        <header className="app-header">
          <div className="wordmark">
            bio<span>workbench</span>
          </div>
          <span className="header-separator" />
          <div className="breadcrumb">
            Workspace
            <ChevronRight size={13} />
            <strong>
              {tab === "prepare"
                ? "New run"
                : tab === "runs"
                  ? "Run history"
                  : "Compare"}
            </strong>
          </div>
          <div className="header-end">
            {demo ? (
              <span className="demo-badge">Local reference example</span>
            ) : (
              <button
                className={`connection-status ${connected ? "online" : ""}`}
                onClick={() => setConnectionOpen(true)}
              >
                <span />
                {loading
                  ? "Connecting…"
                  : connected
                    ? (connection?.host ?? "Head connected")
                    : "Head disconnected"}
              </button>
            )}
            <button
              className="icon-button"
              aria-label="Refresh connection and runs"
              disabled={loading}
              onClick={() => void refresh()}
            >
              <RefreshCw size={16} className={loading ? "spin" : ""} />
            </button>
          </div>
        </header>
        {demo && (
          <div className="demo-notice">
            <FlaskConical size={16} />
            <span>
              Viewer example: both views show the public 1UBQ reference. No
              prediction was run; submission is disabled.
            </span>
            <button
              onClick={() => {
                setDemo(false);
                void refresh();
              }}
            >
              Exit example
              <X size={14} />
            </button>
          </div>
        )}
        {error && (
          <div className="global-error" role="alert">
            <span>{error}</span>
            <button
              className="icon-button"
              aria-label="Dismiss message"
              onClick={() => setError("")}
            >
              <X size={17} />
            </button>
          </div>
        )}
        <main className={`main-content ${tab}`}>
          {tab === "prepare" ? (
            <>
              <div className="page-intro">
                <div>
                  <span className="eyebrow">Your molecular workspace</span>
                  <h1>Build the next experiment.</h1>
                  <p>
                    Bring your constructs. Choose your models. Explore what
                    comes next.
                  </p>
                </div>
                <div className="workspace-note">
                  <ShieldCheck size={20} />
                  <span>
                    Head-hosted records
                    <small>Exact inputs & run provenance retained</small>
                  </span>
                </div>
              </div>
              {!connected && !loading && !demo && (
                <div className="connect-callout">
                  <Plug size={22} />
                  <div>
                    <strong>Connect to your head to get started</strong>
                    <span>
                      You can prepare a local draft while offline, or explore
                      the structure viewer.
                    </span>
                  </div>
                  <button
                    className="button secondary"
                    onClick={() => void showExample()}
                  >
                    Explore viewer
                  </button>
                  <button
                    className="button primary"
                    onClick={() => setConnectionOpen(true)}
                  >
                    Connect head
                    <ArrowRight size={15} />
                  </button>
                </div>
              )}
              <div className="run-name-row">
                <label>
                  Run name
                  <input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. Binder candidates · round 03"
                    maxLength={200}
                  />
                </label>
                <span>
                  {draftSaved
                    ? "Draft saved on this device"
                    : "Local draft could not be saved"}
                </span>
              </div>
              <div className="prepare-grid">
                <InputBuilder
                  inputs={inputs}
                  setInputs={setInputs}
                  draft={editor}
                  setDraft={setEditor}
                  mode={mode}
                  setMode={setMode}
                  upload={(file, progress) => api.upload(file, progress)}
                  connected={connected && !demo}
                  importRequest={importRequest}
                  onImportConsumed={() => setImportRequest(0)}
                />
                <ModelPicker
                  catalog={catalog}
                  inputs={effectiveInputs}
                  mode={mode}
                  selected={models}
                  setSelected={setModels}
                  backend={backend}
                  setBackend={setBackend}
                  execution={execution}
                  setExecution={setExecution}
                  settings={settings}
                  setSettings={setSettings}
                  upload={(file, progress) => api.upload(file, progress)}
                />
              </div>
              <div className="launch-bar">
                <div>
                  <strong>
                    {effectiveInputs.length}{" "}
                    {mode === "assembly" ? "components" : "inputs"}{" "}
                    <span>×</span> {models.length} models
                  </strong>
                  <p>
                    {mode === "assembly"
                      ? "One interacting assembly per selected model."
                      : `Up to ${predictedCount} input/model pairs; the native preview determines the exact set.`}
                  </p>
                </div>
                <button
                  className="button primary large"
                  disabled={busy || !connected || demo}
                  onClick={() => void preview()}
                >
                  {busy ? (
                    <LoaderCircle size={18} className="spin" />
                  ) : (
                    <ShieldCheck size={18} />
                  )}
                  Check compatibility
                  <ArrowRight size={16} />
                </button>
              </div>
              <p className="launch-note">
                Compatibility checks use CPU only. You choose the validated
                pairs before launching predictions.
              </p>
            </>
          ) : tab === "runs" ? (
            <RunsPanel
              runs={runs}
              selectedId={selectedRun}
              onSelect={setSelectedRun}
              onCancel={cancel}
              onCompare={compare}
              onSubmit={submit}
              submitting={busy}
              onLogs={(id) =>
                demo
                  ? Promise.resolve(
                      "This is a local public reference, not a prediction job.",
                    )
                  : api.logs(id)
              }
            />
          ) : (
            <ComparePanel
              artifacts={allArtifacts}
              selected={selectedStructures}
              setSelected={setSelectedStructures}
              loadAnnotations={(artifact) =>
                demo
                  ? Promise.resolve({ annotations: [], deleted_ids: [] })
                  : api.loadAnnotations(artifact)
              }
              loadSharedNotes={(artifact) =>
                demo ? Promise.resolve([]) : api.loadSharedNotes(artifact)
              }
              saveAnnotations={(artifact, notes, deletedIds) =>
                demo
                  ? Promise.reject(
                      new Error("Example annotations stay on this device."),
                    )
                  : api.saveAnnotations(artifact, notes, deletedIds)
              }
            />
          )}
        </main>
        <footer className="app-footer">
          <span>
            Bio Workbench · {window.bioDesktop?.platform ?? "Desktop renderer"}
          </span>
          <span>
            Structures inform hypotheses. Experiments establish function.
          </span>
        </footer>
      </div>
      {connectionOpen && (
        <ConnectionDialog
          connection={connection}
          api={api}
          onClose={() => setConnectionOpen(false)}
          onConnected={refresh}
        />
      )}
    </div>
  );
}
