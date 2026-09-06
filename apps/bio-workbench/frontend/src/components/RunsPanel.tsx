import { ArtifactPreview } from "./ArtifactPreview";
import { useEffect, useState } from "react";
import {
  AlertCircle,
  ArrowUpRight,
  CheckCircle2,
  ChevronRight,
  Clock3,
  FileText,
  LoaderCircle,
  Square,
  XCircle,
} from "lucide-react";
import { bytesLabel, safeArtifactUrl, statusLabels, terminal } from "../domain";
import type { Artifact, Run, RunJob } from "../types";

export function StatusBadge({ status }: { status: Run["status"] }) {
  const Icon =
    status === "succeeded" || status === "validated"
      ? CheckCircle2
      : status === "failed" || status === "validation_failed"
        ? XCircle
        : status === "cancelled"
          ? Square
          : status === "partial"
            ? AlertCircle
            : status === "queued"
              ? Clock3
              : LoaderCircle;
  return (
    <span className={`status-badge ${status}`}>
      <Icon
        size={13}
        className={
          ["running", "validating", "preparing", "cancelling"].includes(status)
            ? "spin"
            : ""
        }
      />
      {statusLabels[status] ?? status}
    </span>
  );
}
function JobCard({
  job,
  onCompare,
  onLogs,
}: {
  job: RunJob;
  onCompare: (artifact: Artifact) => void;
  onLogs: (id: string) => Promise<string>;
}) {
  const [showLogs, setShowLogs] = useState(false);
  const [logs, setLogs] = useState("");
  return (
    <article className="job-card">
      <div className="job-card-header">
        <div>
          <span className="eyebrow">{job.model}</span>
          <h3>{job.name ?? job.id}</h3>
        </div>
        <StatusBadge status={job.status} />
      </div>
      <div className="job-phase">
        <span>{job.phase ?? statusLabels[job.status]}</span>
        {job.message && <p>{job.message}</p>}
      </div>
      {job.error && <div className="inline-error">{job.error}</div>}
      {job.artifacts?.length ? (
        <div className="artifact-list">
          {job.artifacts.map((a) => (
            <div className="artifact-row" key={a.id}>
              <FileText size={16} />
              <div>
                <strong>{a.name}</strong>
                <small>
                  {a.format.toUpperCase()} · {bytesLabel(a.bytes)}
                  {a.selected
                    ? " · QA-selected output"
                    : a.sample_id
                      ? ` · ${a.sample_id}`
                      : ""}
                  {a.chemistry_validation
                    ? ` · QA ${a.chemistry_validation.status}`
                    : ""}
                </small>
              </div>
              {a.kind === "structure" && (
                <button
                  className="button secondary small"
                  onClick={() => onCompare(a)}
                >
                  View 3D
                  <ArrowUpRight size={13} />
                </button>
              )}
              <a
                className="text-link"
                href={safeArtifactUrl(a.url)}
                download={a.name}
              >
                Download
              </a>
              {a.kind !== "structure" && <ArtifactPreview artifact={a} />}
            </div>
          ))}
        </div>
      ) : (
        <p className="empty-note">
          {terminal(job.status)
            ? "No sealed output artifacts are available."
            : "Results appear here as they are finalized."}
        </p>
      )}
      <div className="job-actions">
        <button
          onClick={() => {
            setShowLogs(!showLogs);
            if (!showLogs)
              void onLogs(job.id).then(setLogs, (e) =>
                setLogs(e instanceof Error ? e.message : String(e)),
              );
          }}
        >
          <FileText size={13} />
          {showLogs ? "Hide logs" : "View logs"}
        </button>
        <details>
          <summary>Provenance</summary>
          <pre>{JSON.stringify(job.provenance ?? {}, null, 2)}</pre>
        </details>
      </div>
      {showLogs && (
        <pre className="job-log">{logs || "Loading retained log…"}</pre>
      )}
    </article>
  );
}
export function RunsPanel({
  runs,
  selectedId,
  onSelect,
  onCancel,
  onCompare,
  onSubmit,
  onLogs,
  submitting,
}: {
  runs: Run[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCancel: (id: string) => Promise<void>;
  onCompare: (artifact: Artifact) => void;
  onSubmit: (id: string, pairIds: string[]) => Promise<void>;
  onLogs: (id: string) => Promise<string>;
  submitting: boolean;
}) {
  const selected = runs.find((r) => r.id === selectedId) ?? runs[0];
  const [pairIds, setPairIds] = useState<string[]>([]);
  const [cancelBusy, setCancelBusy] = useState(false);
  useEffect(() => {
    setPairIds(
      selected?.pairs
        ?.filter((p) => p.state === "compatible")
        .map((p) => p.id) ?? [],
    );
  }, [
    selected?.id,
    selected?.pairs?.map((p) => `${p.id}:${p.state}`).join("|"),
  ]);
  const needsValidation =
    selected &&
    ["validating", "validated", "validation_failed"].includes(
      selected.status,
    ) &&
    !selected.jobs.length;
  return (
    <div className="runs-layout">
      <aside className="run-sidebar">
        <div className="inventory-header">
          <strong>Run history</strong>
          <span className="count">{runs.length}</span>
        </div>
        {!runs.length && (
          <div className="empty-small">
            <Clock3 size={25} />
            <span>Your validated inputs and model runs will appear here.</span>
          </div>
        )}
        {runs.map((run) => (
          <button
            key={run.id}
            className={`run-list-item ${run.id === selected?.id ? "selected" : ""}`}
            onClick={() => onSelect(run.id)}
          >
            <div>
              <strong>{run.name}</strong>
              <small>
                {new Date(run.created_at).toLocaleString(undefined, {
                  month: "short",
                  day: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                })}{" "}
                · {run.mode === "assembly" ? "Assembly" : "Batch"}
              </small>
            </div>
            <StatusBadge status={run.status} />
            <ChevronRight size={15} />
          </button>
        ))}
      </aside>
      <div className="run-detail">
        {selected ? (
          <>
            <header className="run-detail-header">
              <div>
                <span className="eyebrow">
                  {needsValidation ? "Input compatibility" : "Run details"} ·{" "}
                  {selected.id.slice(0, 16)}
                </span>
                <h2>{selected.name}</h2>
              </div>
              {!terminal(selected.status) &&
                selected.status !== "validated" && (
                  <button
                    className="button danger small"
                    disabled={cancelBusy || selected.status === "cancelling"}
                    onClick={() => {
                      setCancelBusy(true);
                      void onCancel(selected.id).finally(() =>
                        setCancelBusy(false),
                      );
                    }}
                  >
                    <Square size={13} />
                    {selected.status === "cancelling"
                      ? "Cancellation requested"
                      : "Cancel run"}
                  </button>
                )}
            </header>
            <StatusBadge status={selected.status} />
            {selected.error && (
              <div className="inline-error">{selected.error}</div>
            )}
            {needsValidation && (
              <section className="validation-panel">
                <h3>
                  {selected.status === "validating"
                    ? "Checking every input and model"
                    : "Review compatibility before launch"}
                </h3>
                <p>
                  Native CPU validation checks chemical representation and input
                  support. This step does not run a prediction or an MSA search.
                </p>
                <div className="pair-list">
                  {selected.pairs?.map((pair) => (
                    <label className={`pair-row ${pair.state}`} key={pair.id}>
                      <input
                        type="checkbox"
                        checked={pairIds.includes(pair.id)}
                        disabled={pair.state !== "compatible"}
                        onChange={() =>
                          setPairIds(
                            pairIds.includes(pair.id)
                              ? pairIds.filter((id) => id !== pair.id)
                              : [...pairIds, pair.id],
                          )
                        }
                      />
                      <div>
                        <strong>
                          {pair.input_name}
                          <span> / {pair.model}</span>
                        </strong>
                        <small>
                          {pair.state === "compatible"
                            ? "Compatible"
                            : pair.state === "rejected"
                              ? pair.reasons.join(" · ") || "Not supported"
                              : "Validation in progress"}
                        </small>
                      </div>
                      {pair.state === "compatible" ? (
                        <CheckCircle2 size={17} />
                      ) : pair.state === "rejected" ? (
                        <XCircle size={17} />
                      ) : (
                        <LoaderCircle size={17} className="spin" />
                      )}
                    </label>
                  ))}
                </div>
                {selected.status === "validated" && (
                  <div className="submit-preview">
                    <p>
                      {pairIds.length} selected predictions. Rejected pairs
                      remain in this record.
                    </p>
                    <button
                      className="button primary"
                      disabled={!pairIds.length || submitting}
                      onClick={() => void onSubmit(selected.id, pairIds)}
                    >
                      {submitting ? (
                        <LoaderCircle size={16} className="spin" />
                      ) : (
                        <ArrowUpRight size={16} />
                      )}
                      Launch {pairIds.length} prediction
                      {pairIds.length === 1 ? "" : "s"}
                    </button>
                  </div>
                )}
              </section>
            )}
            <div className="job-grid">
              {selected.jobs.map((job) => (
                <JobCard
                  key={job.id}
                  job={job}
                  onCompare={onCompare}
                  onLogs={onLogs}
                />
              ))}
            </div>
          </>
        ) : (
          <div className="empty-state">
            <Clock3 size={40} />
            <h2>Make room for your next discovery.</h2>
            <p>Prepare an input, check compatible models, then submit a run.</p>
          </div>
        )}
      </div>
    </div>
  );
}
