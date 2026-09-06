import type {
  Annotation,
  AnnotationDocument,
  AnnotationHeadState,
} from "./types";

export function annotationValue(note: Annotation): string {
  const s = note.selection;
  return JSON.stringify([
    note.id,
    note.artifact_sha256,
    s.chain,
    s.resi,
    s.icode ?? "",
    s.resn ?? "",
    s.atom ?? "",
    s.serial ?? null,
    note.label,
    note.note,
    note.color,
  ]);
}

// A successful head read is authoritative. Only locally created IDs that have
// no corresponding head record or tombstone are merged as unsynced additions.
// Divergent local edits/deletions are preserved for export, never replayed.
export function mergeAnnotationState(
  local: AnnotationDocument,
  head: AnnotationHeadState,
) {
  const current = new Map(head.annotations.map((note) => [note.id, note]));
  const deleted = new Set(head.deleted_ids);
  const pending = new Set(local.pending_deletions ?? []);
  const merged = [...head.annotations];
  let conflict = false;
  const localOnly: string[] = [];
  for (const note of local.annotations) {
    const remote = current.get(note.id);
    if (deleted.has(note.id)) {
      conflict = true;
      continue;
    }
    if (remote) {
      if (annotationValue(note) !== annotationValue(remote)) conflict = true;
      continue;
    }
    if (!pending.has(note.id)) {
      merged.push(note);
      localOnly.push(note.id);
    }
  }
  for (const id of pending) if (current.has(id)) conflict = true;
  return { annotations: merged, conflict, local_only_ids: localOnly };
}
