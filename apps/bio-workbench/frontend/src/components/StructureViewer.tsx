import { readLocal, writeLocal } from "../storage";
import { annotationValue, mergeAnnotationState } from "../annotations";
import { useEffect, useRef, useState } from "react";
import {
  Camera,
  CheckCircle2,
  Crosshair,
  Download,
  LoaderCircle,
  MessageSquarePlus,
  RotateCcw,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import type { AtomSpec, GLViewer } from "3dmol";
import {
  confidenceAvailable,
  downloadJson,
  parseAnnotations,
  safeArtifactUrl,
} from "../domain";
import type {
  Annotation,
  AnnotationDocument,
  AnnotationHeadState,
  Artifact,
  ResidueSelection,
  SharedNote,
} from "../types";

export interface ViewerHandle {
  viewer: GLViewer;
  artifact: Artifact;
}
interface Props {
  artifact: Artifact;
  onReady?: (handle: ViewerHandle | null) => void;
  onView?: (view: number[]) => void;
  loadAnnotations: (artifact: Artifact) => Promise<AnnotationHeadState>;
  loadSharedNotes: (artifact: Artifact) => Promise<SharedNote[]>;
  saveAnnotations: (
    artifact: Artifact,
    annotations: Annotation[],
    deletedIds?: string[],
  ) => Promise<void>;
}
const residueKey = (s: ResidueSelection) =>
  `${s.chain}:${s.resi}:${s.icode ?? ""}`;
const atomSelection = (a: AtomSpec): ResidueSelection => ({
  chain: a.chain ?? "",
  resi: a.resi ?? 0,
  icode: a.icode ?? "",
  resn: a.resn ?? "",
  atom: a.atom,
  serial: a.serial,
});
const labelFor = (s: ResidueSelection) =>
  `${s.chain || "∅"} · ${s.resn ?? ""} ${s.resi}${s.icode ?? ""}`;
export function StructureViewer({
  artifact,
  onReady,
  onView,
  loadAnnotations,
  loadSharedNotes,
  saveAnnotations,
}: Props) {
  const container = useRef<HTMLDivElement>(null);
  const viewer = useRef<GLViewer | null>(null);
  const importRef = useRef<HTMLInputElement>(null);
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;
  const onViewRef = useRef(onView);
  onViewRef.current = onView;
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [atoms, setAtoms] = useState<AtomSpec[]>([]);
  const [representation, setRepresentation] = useState("cartoon");
  const [color, setColor] = useState(
    confidenceAvailable(artifact) ? "confidence" : "chain",
  );
  const [selection, setSelection] = useState<ResidueSelection | null>(null);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [note, setNote] = useState("");
  const [label, setLabel] = useState("");
  const [annotationColor, setAnnotationColor] = useState("#e79b41");
  const [saveState, setSaveState] = useState("");
  const [showDetails, setShowDetails] = useState(false);
  const [sharedNotes, setSharedNotes] = useState<SharedNote[]>([]);
  const [notesReady, setNotesReady] = useState(false);
  const [savingNotes, setSavingNotes] = useState(false);
  const [unsyncedIds, setUnsyncedIds] = useState<string[]>([]);
  const [conflictCopies, setConflictCopies] = useState<
    NonNullable<AnnotationDocument["local_conflict_copies"]>
  >([]);
  const pendingDeletions = useRef<string[]>([]);
  const invalidLocalCopy = useRef<string | null>(null);
  const localKey = `bio-workbench.annotations.v1.${artifact.sha256}`;
  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    let observer: ResizeObserver | undefined;
    setLoaded(false);
    setError("");
    setAtoms([]);
    setSelection(null);
    setAnnotations([]);
    setSharedNotes([]);
    setSaveState("");
    setNotesReady(false);
    setUnsyncedIds([]);
    invalidLocalCopy.current = null;
    void (async () => {
      try {
        if (!/^[a-f0-9]{64}$/.test(artifact.sha256))
          throw new Error("This artifact has no valid SHA-256 identity.");
        if (
          !["pdb", "cif", "mmcif", "sdf", "mol2", "xyz"].includes(
            artifact.format.toLowerCase(),
          )
        )
          throw new Error(
            `The ${artifact.format} format is available as a download, but cannot be rendered here.`,
          );
        if ((artifact.bytes ?? 0) > 64 * 1024 * 1024)
          throw new Error(
            "This structure exceeds the 64 MB interactive viewer limit. Download it to open externally.",
          );
        const response = await fetch(safeArtifactUrl(artifact.url), {
          signal: controller.signal,
          credentials: "same-origin",
        });
        if (!response.ok)
          throw new Error(`Structure download failed (${response.status}).`);
        const data = await response.arrayBuffer();
        if (data.byteLength > 64 * 1024 * 1024)
          throw new Error(
            "Structure exceeds the 64 MB interactive viewer limit.",
          );
        const digest = Array.from(
          new Uint8Array(await crypto.subtle.digest("SHA-256", data)),
          (b) => b.toString(16).padStart(2, "0"),
        ).join("");
        if (digest !== artifact.sha256)
          throw new Error(
            "Structure checksum mismatch. The file was not displayed.",
          );
        const mol = await import("3dmol/build/3Dmol.es6.js");
        if (disposed || !container.current) return;
        const v = mol.createViewer(container.current, {
          backgroundColor: "#101b2b",
          antialias: true,
        });
        viewer.current = v;
        v.addModel(
          new TextDecoder().decode(data),
          artifact.format.toLowerCase() === "mmcif"
            ? "cif"
            : artifact.format.toLowerCase(),
          { keepH: true },
        );
        const allAtoms = v.selectedAtoms({});
        if (!allAtoms.length)
          throw new Error("The structure parser found no atoms.");
        setAtoms(allAtoms);
        v.setStyle(
          {},
          { cartoon: { color: "#72c9c1" }, stick: { radius: 0.16 } },
        );
        v.setClickable({}, true, (atom: AtomSpec) => {
          const s = atomSelection(atom);
          setSelection(s);
          setLabel(labelFor(s));
        });
        v.zoomTo();
        v.render();
        v.setViewChangeCallback(() => onViewRef.current?.(v.getView()));
        observer = new ResizeObserver(() => {
          if (!disposed) {
            v.resize();
            v.render();
          }
        });
        observer.observe(container.current);
        setLoaded(true);
        onReadyRef.current?.({ viewer: v, artifact });
      } catch (e) {
        if (!disposed) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    void loadSharedNotes(artifact).then(
      (notes) => {
        if (!disposed) setSharedNotes(notes);
      },
      () => {},
    );
    void (async () => {
      let local: AnnotationDocument = {
        schema: 1,
        kind: "bio-workbench-annotations",
        artifact_sha256: artifact.sha256,
        annotations: [],
      };
      let stored: string | null = null;
      try {
        stored = readLocal(localKey);
        if (stored)
          local = parseAnnotations(JSON.parse(stored), artifact.sha256);
      } catch {
        // Keep unreadable source bytes exportable and never overwrite them.
        invalidLocalCopy.current = stored;
      }
      if (!disposed) {
        setAnnotations(local.annotations);
        setConflictCopies(local.local_conflict_copies ?? []);
        pendingDeletions.current = local.pending_deletions ?? [];
      }
      try {
        const shared = await loadAnnotations(artifact);
        parseAnnotations(
          {
            schema: 1,
            kind: local.kind,
            artifact_sha256: artifact.sha256,
            annotations: shared.annotations,
          },
          artifact.sha256,
        );
        if (disposed) return;
        const merged = mergeAnnotationState(local, shared);
        const copies = [...(local.local_conflict_copies ?? [])];
        if (merged.conflict)
          copies.push({
            annotations: local.annotations,
            pending_deletions: local.pending_deletions ?? [],
            captured_at: new Date().toISOString(),
          });
        setAnnotations(merged.annotations);
        setConflictCopies(copies);
        pendingDeletions.current = [];
        setUnsyncedIds(merged.local_only_ids);
        if (merged.conflict)
          setSaveState(
            "Head versions loaded. Conflicting local notes or pending deletions were preserved for export and were not applied to the head.",
          );
        else if (merged.local_only_ids.length)
          setSaveState(
            "Local additions retained alongside current head annotations. No edits were automatically sent.",
          );
        try {
          if (invalidLocalCopy.current)
            throw new Error("Unreadable local copy preserved");
          const document = parseAnnotations(
            {
              ...local,
              annotations: merged.annotations,
              pending_deletions: [],
              local_conflict_copies: copies,
            },
            artifact.sha256,
          );
          writeLocal(localKey, JSON.stringify(document));
        } catch {
          setSaveState(
            "Head versions loaded; the previous local copy was not overwritten because it could not be safely replaced. Export notes and preserved copies to keep this view.",
          );
        }
      } catch {
        if (!disposed)
          setSaveState("Local annotations restored; head copy unavailable.");
      } finally {
        if (!disposed) setNotesReady(true);
      }
    })();
    return () => {
      disposed = true;
      controller.abort();
      observer?.disconnect();
      if (viewer.current) {
        viewer.current.setViewChangeCallback(null);
        viewer.current.clear();
        viewer.current = null;
      }
      onReadyRef.current?.(null);
      container.current?.replaceChildren();
    };
  }, [artifact.id, artifact.sha256, artifact.url]);
  useEffect(() => {
    const v = viewer.current;
    if (!v || !loaded) return;
    const palette = [
      "#70c8c0",
      "#9cacf5",
      "#eac779",
      "#f095a9",
      "#a6ce78",
      "#cda5e8",
    ];
    const chainColors = new Map(
      [...new Set(atoms.map((a) => a.chain ?? ""))].map((chain, i) => [
        chain,
        palette[i % palette.length],
      ]),
    );
    const colorfunc = (a: AtomSpec) => {
      if (color === "confidence" && confidenceAvailable(artifact)) {
        const p = a.b ?? 0;
        return p >= 90
          ? "#2487ed"
          : p >= 70
            ? "#63c8e5"
            : p >= 50
              ? "#f4d563"
              : "#ed9155";
      }
      if (color === "element") return undefined;
      return chainColors.get(a.chain ?? "") ?? palette[0];
    };
    v.removeAllSurfaces();
    const style = color === "element" ? { colorscheme: "Jmol" } : { colorfunc };
    if (representation === "sticks")
      v.setStyle({}, { stick: { ...style, radius: 0.18 } });
    else if (representation === "spheres")
      v.setStyle({}, { sphere: { ...style, scale: 0.65 } });
    else {
      v.setStyle({}, { cartoon: { ...style, arrows: true } });
      v.addStyle({ hetflag: true }, { stick: { ...style, radius: 0.2 } });
      if (!atoms.some((a) => a.atom === "CA" || a.atom === "P"))
        v.addStyle({}, { stick: { ...style, radius: 0.2 } });
    }
    if (selection)
      v.addStyle(
        {
          chain: selection.chain,
          resi: selection.resi,
          icode: selection.icode,
        },
        {
          stick: { color: "#f4ba60", radius: 0.23 },
          sphere: { color: "#f4ba60", scale: 0.24 },
        },
      );
    v.removeAllLabels();
    for (const annotation of annotations) {
      const target = atoms.find(
        (a) =>
          residueKey(atomSelection(a)) === residueKey(annotation.selection) &&
          (!annotation.selection.atom || a.atom === annotation.selection.atom),
      );
      if (
        target &&
        target.x !== undefined &&
        target.y !== undefined &&
        target.z !== undefined
      )
        v.addLabel(annotation.label, {
          position: { x: target.x, y: target.y, z: target.z },
          fontSize: 13,
          backgroundColor: "#18263a",
          backgroundOpacity: 0.88,
          fontColor: annotation.color,
          borderColor: annotation.color,
          borderThickness: 1,
          inFront: true,
        });
    }
    v.render();
  }, [loaded, representation, color, selection, annotations, atoms, artifact]);
  const residues = Array.from(
    new Map(
      atoms
        .filter((a) => a.resi !== undefined)
        .map((a) => [residueKey(atomSelection(a)), atomSelection(a)]),
    ).values(),
  );
  async function persist(
    next: Annotation[],
    deletedIds: string[] = [],
    forceIds: string[] = [],
  ) {
    if (!notesReady || savingNotes) return;
    setSavingNotes(true);
    const changed = next.filter(
      (note) =>
        forceIds.includes(note.id) ||
        !annotations.some(
          (old) => annotationValue(old) === annotationValue(note),
        ),
    );
    setAnnotations(next);
    pendingDeletions.current = [
      ...new Set([...pendingDeletions.current, ...deletedIds]),
    ];
    const doc: AnnotationDocument = {
      schema: 1,
      kind: "bio-workbench-annotations",
      artifact_sha256: artifact.sha256,
      annotations: next,
      pending_deletions: pendingDeletions.current,
      local_conflict_copies: conflictCopies,
    };
    let localSaved = false;
    try {
      if (invalidLocalCopy.current)
        throw new Error("Unreadable local copy preserved");
      parseAnnotations(doc, artifact.sha256);
      writeLocal(localKey, JSON.stringify(doc));
      localSaved = true;
      setSaveState("Saved on this device · syncing…");
    } catch {
      setSaveState(
        "Local storage unavailable. Export a copy to preserve notes.",
      );
    }
    try {
      await saveAnnotations(artifact, changed, deletedIds);
      const remainingUnsynced = unsyncedIds.filter(
        (id) =>
          !changed.some((note) => note.id === id) && !deletedIds.includes(id),
      );
      setUnsyncedIds(remainingUnsynced);
      pendingDeletions.current = pendingDeletions.current.filter(
        (id) => !deletedIds.includes(id),
      );
      try {
        if (invalidLocalCopy.current)
          throw new Error("Unreadable local copy preserved");
        parseAnnotations(doc, artifact.sha256);
        writeLocal(
          localKey,
          JSON.stringify({
            ...doc,
            pending_deletions: pendingDeletions.current,
          }),
        );
      } catch {
        localSaved = false;
      }
      setSaveState(
        pendingDeletions.current.length || remainingUnsynced.length
          ? "This change synced; other local additions or deletions still need review. Export includes all pending changes."
          : localSaved
            ? "Saved on this device and head"
            : "Saved on the head; local storage unavailable.",
      );
    } catch (e) {
      setUnsyncedIds((ids) =>
        [...new Set([...ids, ...changed.map((note) => note.id)])].filter(
          (id) => !deletedIds.includes(id),
        ),
      );
      setSaveState(
        `${localSaved ? "Saved on this device" : "Not saved; export a copy to preserve notes"}; head sync failed: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setSavingNotes(false);
    }
  }
  const addAnnotation = () => {
    if (selection && label.trim()) {
      void persist([
        ...annotations,
        {
          id: crypto.randomUUID(),
          artifact_sha256: artifact.sha256,
          selection,
          label: label.trim().slice(0, 160),
          note,
          color: annotationColor,
          created_at: new Date().toISOString(),
        },
      ]);
      setNote("");
    }
  };
  async function importNotes(file?: File) {
    if (!file) return;
    try {
      if (file.size > 2 * 1024 * 1024)
        throw new Error("Annotation file exceeds 2 MB.");
      const doc = parseAnnotations(
        JSON.parse(await file.text()),
        artifact.sha256,
      );
      const incoming = new Map(doc.annotations.map((note) => [note.id, note]));
      await persist([
        ...annotations.filter((note) => !incoming.has(note.id)),
        ...doc.annotations,
      ]);
    } catch (e) {
      setSaveState(e instanceof Error ? e.message : String(e));
    }
  }
  const exportPng = () => {
    const uri = viewer.current?.pngURI();
    if (!uri?.startsWith("data:image/png;base64,")) return;
    const raw = atob(uri.slice("data:image/png;base64,".length));
    const bytes = Uint8Array.from(raw, (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: "image/png" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${artifact.name}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <article
      className="structure-card"
      data-testid={`structure-${artifact.id}`}
    >
      <header>
        <div>
          <span className="eyebrow">
            {artifact.model ?? "Structure"}
            {artifact.selected
              ? " · selected output"
              : artifact.sample_id
                ? ` · ${artifact.sample_id}`
                : artifact.sample_index === undefined
                  ? ""
                  : ` · sample ${artifact.sample_index + 1}`}
          </span>
          <h3>{artifact.name}</h3>
        </div>
        <span
          className={`qa-badge ${artifact.chemistry_validation?.status === "passed" ? "passed" : ""}`}
          title={artifact.chemistry_validation?.message}
        >
          {artifact.chemistry_validation?.status === "passed" ? (
            <>
              <CheckCircle2 size={13} />
              Chemistry checked
            </>
          ) : (
            (artifact.chemistry_validation?.status ?? "QA not reported")
          )}
        </span>
      </header>
      <div className="viewer-toolbar">
        <label className="sr-only" htmlFor={`representation-${artifact.id}`}>
          Representation for {artifact.name}
        </label>
        <select
          id={`representation-${artifact.id}`}
          value={representation}
          onChange={(e) => setRepresentation(e.target.value)}
        >
          <option value="cartoon">Cartoon + ligands</option>
          <option value="sticks">Sticks</option>
          <option value="spheres">Spheres</option>
        </select>
        <label className="sr-only" htmlFor={`color-${artifact.id}`}>
          Color for {artifact.name}
        </label>
        <select
          id={`color-${artifact.id}`}
          value={color}
          onChange={(e) => setColor(e.target.value)}
        >
          <option value="chain">By chain</option>
          <option value="element">By element</option>
          {confidenceAvailable(artifact) && (
            <option value="confidence">By pLDDT</option>
          )}
        </select>
        <div className="toolbar-spacer" />
        <button
          className="icon-button"
          aria-label={`Reset view ${artifact.name}`}
          title="Reset view"
          onClick={() => {
            viewer.current?.zoomTo();
            viewer.current?.render();
          }}
        >
          <RotateCcw size={16} />
        </button>
        <button
          className="icon-button"
          disabled={!loaded}
          aria-label={`Save image ${artifact.name}`}
          title="Save image"
          onClick={exportPng}
        >
          <Camera size={16} />
        </button>
        <a
          className="icon-button"
          href={safeArtifactUrl(artifact.url)}
          download={artifact.name}
          title="Download original structure"
          aria-label={`Download ${artifact.name}`}
        >
          <Download size={16} />
        </a>
      </div>
      <div className="viewer-stage">
        <div
          className="molecule-canvas"
          ref={container}
          aria-label={`Interactive structure ${artifact.name}`}
          role="img"
        />
        {!loaded && (
          <div className={`viewer-overlay ${error ? "error" : ""}`}>
            {error ? (
              <>
                <X size={24} />
                <strong>Could not display structure</strong>
                <span>{error}</span>
              </>
            ) : (
              <>
                <LoaderCircle className="spin" size={25} />
                <span>Verifying and loading structure…</span>
              </>
            )}
          </div>
        )}
        <div className="viewer-hint">
          <Crosshair size={13} />
          Drag to rotate · scroll to zoom · click a residue
        </div>
        {loaded && (
          <div className="atom-count">
            {atoms.length.toLocaleString()} atoms ·{" "}
            {new Set(atoms.map((a) => a.chain)).size} chains
          </div>
        )}
      </div>
      {confidenceAvailable(artifact) && (
        <div className="confidence-legend">
          <span>pLDDT</span>
          <i style={{ background: "#ed9155" }} />
          <span>&lt;50</span>
          <i style={{ background: "#f4d563" }} />
          <span>50–70</span>
          <i style={{ background: "#63c8e5" }} />
          <span>70–90</span>
          <i style={{ background: "#2487ed" }} />
          <span>90–100</span>
        </div>
      )}
      {artifact.confidence?.metrics && (
        <dl className="native-metrics">
          {Object.entries(artifact.confidence.metrics).map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>
                {typeof value === "boolean"
                  ? String(value)
                  : Number.isFinite(value)
                    ? value.toFixed(3)
                    : "unavailable"}
              </dd>
            </div>
          ))}
        </dl>
      )}
      <div className="residue-bar">
        <Crosshair size={15} />
        <select
          aria-label={`Select residue in ${artifact.name}`}
          value={selection ? residueKey(selection) : ""}
          onChange={(e) => {
            const s =
              residues.find((r) => residueKey(r) === e.target.value) ?? null;
            setSelection(s);
            if (s) setLabel(labelFor(s));
          }}
        >
          <option value="">Pick a residue to inspect or annotate</option>
          {residues.map((r) => (
            <option key={residueKey(r)} value={residueKey(r)}>
              {labelFor(r)}
            </option>
          ))}
        </select>
        {selection && (
          <button
            className="icon-button"
            title="Center selection"
            onClick={() => {
              viewer.current?.zoomTo({
                chain: selection.chain,
                resi: selection.resi,
                icode: selection.icode,
              });
              viewer.current?.render();
            }}
          >
            <Crosshair size={15} />
          </button>
        )}
      </div>
      {selection && (
        <div className="annotation-editor">
          <div className="field-row">
            <label className="grow">
              Residue label
              <input
                aria-label={`Label for ${artifact.name}`}
                value={label}
                maxLength={160}
                onChange={(e) => setLabel(e.target.value)}
              />
            </label>
            <label>
              Color
              <input
                type="color"
                aria-label={`Label color for ${artifact.name}`}
                value={annotationColor}
                onChange={(e) => setAnnotationColor(e.target.value)}
              />
            </label>
          </div>
          <textarea
            aria-label={`Annotation note for ${artifact.name}`}
            placeholder="Describe a binding site, design constraint, or question to investigate…"
            value={note}
            maxLength={10000}
            onChange={(e) => setNote(e.target.value)}
          />
          <button
            className="button secondary small"
            disabled={!label.trim() || !notesReady || savingNotes}
            onClick={addAnnotation}
          >
            <MessageSquarePlus size={14} />
            Save annotation
          </button>
        </div>
      )}
      <div className="annotation-section">
        <div className="inventory-header">
          <strong>
            Annotations <span className="count">{annotations.length}</span>
          </strong>
          <div>
            <button
              className="icon-button"
              title="Import annotations for this exact artifact"
              aria-label={`Import annotations ${artifact.name}`}
              disabled={!notesReady || savingNotes}
              onClick={() => importRef.current?.click()}
            >
              <Upload size={14} />
            </button>
            <button
              className="icon-button"
              title="Export annotations"
              aria-label={`Export annotations ${artifact.name}`}
              onClick={() =>
                downloadJson(
                  {
                    schema: 1,
                    kind: "bio-workbench-annotations",
                    artifact_sha256: artifact.sha256,
                    annotations,
                    pending_deletions: pendingDeletions.current,
                    local_conflict_copies: conflictCopies,
                  },
                  `annotations-${artifact.sha256.slice(0, 12)}.json`,
                )
              }
            >
              <Download size={14} />
            </button>
            <input
              type="file"
              ref={importRef}
              hidden
              accept=".json"
              onChange={(e) => void importNotes(e.target.files?.[0])}
            />
          </div>
        </div>
        {annotations.map((a) => (
          <div className="annotation-row" key={a.id}>
            <span style={{ background: a.color }} />
            <button
              onClick={() => {
                setSelection(a.selection);
                setLabel(a.label);
              }}
            >
              <strong>{a.label}</strong>
              {a.note && <small>{a.note}</small>}
            </button>
            <button
              className="icon-button"
              aria-label={`Delete annotation ${a.label}`}
              disabled={!notesReady || savingNotes}
              onClick={() =>
                void persist(
                  annotations.filter((n) => n.id !== a.id),
                  [a.id],
                )
              }
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))}
        {!annotations.length && (
          <p className="empty-note">
            Select a residue to attach a label and a research note.
          </p>
        )}
        {saveState && (
          <p className="save-state" role="status">
            {saveState}
          </p>
        )}
        {invalidLocalCopy.current && (
          <button
            className="button secondary small"
            onClick={() => {
              const url = URL.createObjectURL(
                new Blob([invalidLocalCopy.current!], {
                  type: "application/json",
                }),
              );
              const link = document.createElement("a");
              link.href = url;
              link.download = `preserved-local-annotations-${artifact.sha256.slice(0, 12)}.json`;
              link.click();
              setTimeout(() => URL.revokeObjectURL(url), 1000);
            }}
          >
            Export unreadable original local copy
          </button>
        )}
        {conflictCopies.length > 0 && (
          <>
            <p className="save-state">
              Local versions differed from the head. Current head notes are
              shown; the original local copies remain available for review.
            </p>
            <button
              className="button secondary small"
              onClick={() =>
                downloadJson(
                  {
                    schema: 1,
                    kind: "bio-workbench-local-annotation-conflicts",
                    artifact_sha256: artifact.sha256,
                    copies: conflictCopies,
                  },
                  `local-annotation-conflicts-${artifact.sha256.slice(0, 12)}.json`,
                )
              }
            >
              Export preserved local copies ({conflictCopies.length})
            </button>
          </>
        )}
        {pendingDeletions.current.length > 0 && (
          <p className="save-state">
            {pendingDeletions.current.length} local deletions are not confirmed
            on the head. Reopen this structure to review the current head notes
            before deleting them.
          </p>
        )}
        {unsyncedIds.length > 0 && (
          <button
            className="button secondary small"
            disabled={!notesReady || savingNotes}
            onClick={() => void persist(annotations, [], unsyncedIds)}
          >
            Sync local additions ({unsyncedIds.length})
          </button>
        )}
      </div>
      {sharedNotes.length > 0 && (
        <section className="shared-notes">
          <h3>Shared research notes</h3>
          {sharedNotes.map((n) => (
            <article key={n.id}>
              <small>
                {n.author} · {new Date(n.updated_at).toLocaleString()}
              </small>
              <p>{n.text}</p>
              {n.selection != null && (
                <details>
                  <summary>Recorded selection</summary>
                  <pre>{JSON.stringify(n.selection, null, 2)}</pre>
                </details>
              )}
            </article>
          ))}
        </section>
      )}
      <button
        className="provenance-toggle"
        onClick={() => setShowDetails(!showDetails)}
        aria-expanded={showDetails}
      >
        Source & provenance <span>{artifact.sha256.slice(0, 12)}…</span>
      </button>
      {showDetails && (
        <div className="provenance-detail">
          <p>
            Display changes and annotations do not change the original
            coordinates.
          </p>
          {artifact.confidence && (
            <p>
              {artifact.confidence.kind} · {artifact.confidence.source}
              {artifact.confidence.mean === undefined
                ? ""
                : ` · mean ${artifact.confidence.mean.toFixed(2)}`}
            </p>
          )}
          <pre>
            {JSON.stringify(
              {
                sha256: artifact.sha256,
                chain_mapping: artifact.chain_mapping,
                chemistry_validation: artifact.chemistry_validation,
                provenance: artifact.provenance,
              },
              null,
              2,
            )}
          </pre>
        </div>
      )}
    </article>
  );
}
