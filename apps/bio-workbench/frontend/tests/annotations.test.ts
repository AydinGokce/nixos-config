import { expect, test } from "vitest";
import { mergeAnnotationState } from "../src/annotations";
import { WorkbenchApi } from "../src/api";
import type { Annotation, AnnotationDocument, Artifact } from "../src/types";

const hash = "a".repeat(64);
const note = (id: string, text = id): Annotation => ({
  id,
  artifact_sha256: hash,
  selection: { chain: "A", resi: 4 },
  label: id,
  note: text,
  color: "#aabbcc",
  created_at: "2026-09-06T00:00:00Z",
});
const doc = (
  annotations: Annotation[],
  pending_deletions: string[] = [],
): AnnotationDocument => ({
  schema: 1,
  kind: "bio-workbench-annotations",
  artifact_sha256: hash,
  annotations,
  pending_deletions,
});
const artifact = { id: "structure", sha256: hash } as Artifact;
const raw = (n: Annotation, revision = 1) => ({
  annotation_id: `head-${n.id}`,
  artifact_id: "structure",
  revision,
  text: `${n.label}\n\n${n.note}`,
  selection: {
    kind: "bio-workbench-residue-v1",
    local_id: n.id,
    artifact_sha256: hash,
    residue: n.selection,
    label: n.label,
    note: n.note,
    color: n.color,
    deleted: false,
  },
  created_at: n.created_at,
});

test("head edits and new remote notes win while unsynced local additions survive", () => {
  const local = doc([note("same", "offline edit"), note("local-new")]);
  const head = {
    annotations: [note("same", "newer head edit"), note("remote-new")],
    deleted_ids: [],
  };
  const merged = mergeAnnotationState(local, head);
  expect(merged.annotations.map((n) => [n.id, n.note])).toEqual([
    ["same", "newer head edit"],
    ["remote-new", "remote-new"],
    ["local-new", "local-new"],
  ]);
  expect(merged.conflict).toBe(true);
  expect(merged.local_only_ids).toEqual(["local-new"]);
  expect(local.annotations[0].note).toBe("offline edit");
});
test("head tombstones prevent resurrection and offline deletions are never replayed", () => {
  const merged = mergeAnnotationState(
    doc([note("deleted")], ["still-on-head"]),
    {
      annotations: [note("still-on-head", "edited elsewhere")],
      deleted_ids: ["deleted"],
    },
  );
  expect(merged.annotations.map((n) => n.id)).toEqual(["still-on-head"]);
  expect(merged.conflict).toBe(true);
  expect(merged.local_only_ids).toEqual([]);
});
test("server timestamp changes alone are not annotation conflicts", () => {
  expect(
    mergeAnnotationState(doc([note("same")]), {
      annotations: [{ ...note("same"), created_at: "2026-09-07T00:00:00Z" }],
      deleted_ids: [],
    }).conflict,
  ).toBe(false);
});
function fakeHead(initial = [raw(note("a")), raw(note("b"))]) {
  const rows = new Map(
    initial.map((r) => [r.annotation_id, structuredClone(r)]),
  );
  const writes: any[] = [];
  const rpc = async (method: string, params: any = {}) => {
    if (method === "annotation.list")
      return { annotations: structuredClone([...rows.values()]) } as any;
    if (method !== "annotation.put") throw new Error(method);
    writes.push(structuredClone(params));
    const old = params.annotation_id
      ? rows.get(params.annotation_id)
      : undefined;
    if (old && params.expected_revision !== old.revision)
      throw new Error("conflict: another device changed this annotation");
    const value = {
      ...params,
      annotation_id: old?.annotation_id ?? `head-${params.selection.local_id}`,
      revision: old ? old.revision + 1 : 1,
      created_at: old?.created_at ?? "2026-09-06T00:00:00Z",
    };
    rows.set(value.annotation_id, value);
    return structuredClone(value) as any;
  };
  return { rows, writes, rpc };
}
test("two devices retain their observed revisions and refuse a stale write", async () => {
  const head = fakeHead();
  const a = new WorkbenchApi();
  const b = new WorkbenchApi();
  a.rpc = head.rpc;
  b.rpc = head.rpc;
  await a.loadAnnotations(artifact);
  await b.loadAnnotations(artifact);
  await b.saveAnnotations(artifact, [note("a", "new head text")]);
  await expect(
    a.saveAnnotations(artifact, [note("a", "stale local text")]),
  ).rejects.toThrow("conflict");
  expect(head.rows.get("head-a")!.selection.note).toBe("new head text");
  expect(head.rows.get("head-b")!.selection.deleted).toBe(false);
});
test("saving one note never deletes absent notes; deletion requires its explicit ID", async () => {
  const head = fakeHead();
  const api = new WorkbenchApi();
  api.rpc = head.rpc;
  await api.loadAnnotations(artifact);
  await api.saveAnnotations(artifact, [note("a", "changed")]);
  expect(head.writes).toHaveLength(1);
  expect(head.rows.get("head-b")!.selection.deleted).toBe(false);
  await api.saveAnnotations(artifact, [], ["a"]);
  expect(head.rows.get("head-a")!.selection.deleted).toBe(true);
  expect(head.rows.get("head-b")!.selection.deleted).toBe(false);
});
test("offline edits cannot acquire current revisions implicitly when connection returns", async () => {
  const head = fakeHead([raw(note("a", "updated while offline"), 8)]);
  const api = new WorkbenchApi();
  api.rpc = async () => {
    throw new Error("offline");
  };
  await expect(api.loadAnnotations(artifact)).rejects.toThrow("offline");
  api.rpc = head.rpc;
  await expect(
    api.saveAnnotations(artifact, [note("a", "stale offline edit")]),
  ).rejects.toThrow("Reopen this structure");
  await expect(api.saveAnnotations(artifact, [], ["a"])).rejects.toThrow(
    "Reopen this structure",
  );
  expect(head.writes).toHaveLength(0);
  expect(head.rows.get("head-a")!.revision).toBe(8);
  await api.loadAnnotations(artifact);
  await api.saveAnnotations(artifact, [note("new-local")]);
  expect(head.rows.get("head-a")!.selection.note).toBe("updated while offline");
  expect(head.rows.has("head-new-local")).toBe(true);
});

test("syncing a new offline note never authorizes a later stale existing edit", async () => {
  const head = fakeHead([raw(note("a", "new remote text"), 8)]);
  const api = new WorkbenchApi();
  api.rpc = head.rpc;
  await api.saveAnnotations(artifact, [note("new-local")]);
  await expect(
    api.saveAnnotations(artifact, [note("a", "stale local edit")]),
  ).rejects.toThrow("Reopen this structure");
  await expect(api.saveAnnotations(artifact, [], ["a"])).rejects.toThrow(
    "Reopen this structure",
  );
  expect(head.writes).toHaveLength(1);
  expect(head.rows.get("head-a")!.selection.note).toBe("new remote text");
  // Own writes still carry their observed revision for later deliberate edits.
  await api.saveAnnotations(artifact, [
    note("new-local", "own subsequent edit"),
  ]);
  expect(head.rows.get("head-new-local")!.revision).toBe(2);
});

test("an obsolete load cannot advance the revision baseline displayed by a newer view", async () => {
  const head = fakeHead([raw(note("a", "visible rev8"), 8)]);
  const api = new WorkbenchApi();
  let release!: (value: any) => void;
  let first = true;
  api.rpc = async (method, params) => {
    if (method === "annotation.list" && first) {
      first = false;
      return (await new Promise((resolve) => {
        release = resolve;
      })) as any;
    }
    return head.rpc(method, params);
  };
  const obsolete = api.loadAnnotations(artifact).catch((e) => e);
  const visible = await api.loadAnnotations(artifact);
  expect(visible.annotations[0].note).toBe("visible rev8");
  head.rows.set("head-a", raw(note("a", "remote rev9"), 9));
  release({ annotations: [raw(note("a", "remote rev9"), 9)] });
  expect((await obsolete).message).toContain("superseded");
  await expect(
    api.saveAnnotations(artifact, [note("a", "stale displayed rev8")]),
  ).rejects.toThrow("conflict");
  expect(head.rows.get("head-a")!.selection.note).toBe("remote rev9");
});

test("a failed newer read still invalidates every older pending baseline", async () => {
  const head = fakeHead([raw(note("a", "remote rev9"), 9)]);
  const api = new WorkbenchApi();
  let release!: (value: any) => void;
  let calls = 0;
  api.rpc = async () => {
    if (calls++ === 0)
      return (await new Promise((resolve) => {
        release = resolve;
      })) as any;
    throw new Error("offline");
  };
  const obsolete = api.loadAnnotations(artifact).catch((e) => e);
  await expect(api.loadAnnotations(artifact)).rejects.toThrow("offline");
  release({ annotations: [raw(note("a", "remote rev9"), 9)] });
  expect((await obsolete).message).toContain("superseded");
  api.rpc = head.rpc;
  await expect(
    api.saveAnnotations(artifact, [note("a", "unseen")]),
  ).rejects.toThrow("Reopen this structure");
  expect(head.writes).toHaveLength(0);
});
