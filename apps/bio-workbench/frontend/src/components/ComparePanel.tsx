import { useRef, useState } from "react";
import { Boxes, Columns2, Link2, Plus, Unlink, X } from "lucide-react";
import type {
  Artifact,
  Annotation,
  AnnotationHeadState,
  SharedNote,
} from "../types";
import { StructureViewer, type ViewerHandle } from "./StructureViewer";
export function ComparePanel({
  artifacts,
  selected,
  setSelected,
  loadAnnotations,
  loadSharedNotes,
  saveAnnotations,
}: {
  artifacts: Artifact[];
  selected: string[];
  setSelected: (ids: string[]) => void;
  loadAnnotations: (a: Artifact) => Promise<AnnotationHeadState>;
  loadSharedNotes: (a: Artifact) => Promise<SharedNote[]>;
  saveAnnotations: (
    a: Artifact,
    notes: Annotation[],
    deletedIds?: string[],
  ) => Promise<void>;
}) {
  const [linked, setLinked] = useState(false);
  const viewers = useRef(new Map<string, ViewerHandle>());
  const syncing = useRef(false);
  const [manualChoice, setManualChoice] = useState("");
  const structures = artifacts.filter((a) => a.kind === "structure");
  const shown = selected
    .map((id) => structures.find((a) => a.id === id))
    .filter((a): a is Artifact => !!a);
  const preferred = structures.filter(
    (a) => a.selected === true && a.chemistry_validation?.status === "passed",
  );
  function synchronize(id: string, view: number[]) {
    if (!linked || syncing.current) return;
    syncing.current = true;
    try {
      for (const [otherId, handle] of viewers.current)
        if (otherId !== id) {
          // Link rotation/zoom only; retain each structure's own center.
          const own = handle.viewer.getView();
          handle.viewer.setView([...own.slice(0, 3), ...view.slice(3)], true);
        }
    } finally {
      syncing.current = false;
    }
  }
  return (
    <div className="comparison-workspace">
      <header className="comparison-header">
        <div>
          <span className="eyebrow">Inspect · compare · annotate</span>
          <h2>Structure comparison</h2>
          <p>
            All samples remain available. Linking cameras does not structurally
            align models.
          </p>
        </div>
        <div className="comparison-actions">
          <button
            className={`button secondary ${linked ? "is-on" : ""}`}
            aria-pressed={linked}
            onClick={() => setLinked(!linked)}
          >
            {linked ? <Link2 size={16} /> : <Unlink size={16} />}Link cameras
          </button>
          <label className="add-view">
            <span className="sr-only">Choose a structure to add</span>
            <select
              value={manualChoice}
              onChange={(e) => setManualChoice(e.target.value)}
            >
              <option value="">Choose a structure…</option>
              {structures
                .filter((a) => !selected.includes(a.id))
                .map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.model} · {a.name}
                    {a.selected ? " · selected" : ""}
                  </option>
                ))}
            </select>
            <button
              className="button secondary"
              disabled={
                shown.length >= 4 ||
                !manualChoice ||
                selected.includes(manualChoice)
              }
              onClick={() => {
                setSelected([...selected, manualChoice]);
                setManualChoice("");
              }}
            >
              <Plus size={16} />
              Add view
            </button>
          </label>
        </div>
      </header>
      {!shown.length ? (
        <div className="empty-state">
          <Columns2 size={42} />
          <h2>A clearer view of your results.</h2>
          <p>
            Choose a structure from a run to rotate it, inspect residues, and
            compare samples. Raw samples remain individually selectable.
          </p>
          {preferred.length > 0 && (
            <button
              className="button primary"
              onClick={() =>
                setSelected(preferred.slice(0, 2).map((a) => a.id))
              }
            >
              <Boxes size={16} />
              Open QA-selected outputs
            </button>
          )}
        </div>
      ) : (
        <div className={`structure-grid count-${shown.length}`}>
          {shown.map((artifact) => (
            <div className="structure-slot" key={artifact.id}>
              <div className="structure-selector">
                <select
                  aria-label={`Structure in view ${shown.indexOf(artifact) + 1}`}
                  value={artifact.id}
                  onChange={(e) =>
                    setSelected(
                      selected.map((id) =>
                        id === artifact.id ? e.target.value : id,
                      ),
                    )
                  }
                >
                  {structures
                    .filter(
                      (a) => a.id === artifact.id || !selected.includes(a.id),
                    )
                    .map((a) => (
                      <option value={a.id} key={a.id}>
                        {a.model ?? "Structure"} · {a.name}
                        {a.selected ? " · selected output" : ""}
                      </option>
                    ))}
                </select>
                <button
                  className="icon-button"
                  aria-label={`Remove view ${artifact.name}`}
                  onClick={() =>
                    setSelected(selected.filter((id) => id !== artifact.id))
                  }
                >
                  <X size={16} />
                </button>
              </div>
              <StructureViewer
                artifact={artifact}
                onReady={(handle) => {
                  if (handle) viewers.current.set(artifact.id, handle);
                  else viewers.current.delete(artifact.id);
                }}
                onView={(view) => synchronize(artifact.id, view)}
                loadAnnotations={loadAnnotations}
                loadSharedNotes={loadSharedNotes}
                saveAnnotations={saveAnnotations}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
