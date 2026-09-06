import type {
  Annotation,
  AnnotationHeadState,
  Artifact,
  Catalog,
  CreateRun,
  InputFormat,
  Run,
  RunJob,
  RunStatus,
  SharedNote,
  UploadReceipt,
} from "./types";
import { parseAnnotations } from "./domain";

export interface Connection {
  host: string;
  user: string;
  port: number;
  key_path?: string;
  configured: boolean;
}
export interface Session {
  csrf_token: string;
  connection: Connection;
}
export class ApiError extends Error {
  constructor(
    message: string,
    public code = "unavailable",
  ) {
    super(message);
  }
}
type Raw = Record<string, any>;
const state = (s: string): RunStatus =>
  (
    ({
      complete: "succeeded",
      validating: "validating",
      starting: "preparing",
      cancel_requested: "cancelling",
      interrupted: "failed",
    }) as Record<string, RunStatus>
  )[s] ?? (s as RunStatus);
export function mapArtifact(a: Raw): Artifact {
  const confidence = a.confidence
    ? {
        ...a.confidence,
        kind: a.confidence.kind ?? a.confidence.metric ?? "Native confidence",
        source:
          typeof a.confidence.source === "string"
            ? a.confidence.source
            : "Native result",
      }
    : undefined;
  return {
    id: a.artifact_id,
    name: a.name,
    kind:
      a.role === "structure"
        ? "structure"
        : /csv|tsv/.test(a.format)
          ? "table"
          : /fasta|fa$/.test(a.format)
            ? "sequence"
            : "file",
    format: a.format,
    url: `/api/v1/artifacts/${encodeURIComponent(a.artifact_id)}`,
    sha256: a.sha256,
    bytes: a.size,
    model: a.model,
    sample_index: typeof a.sample_id === "number" ? a.sample_id : undefined,
    sample_id: a.sample_id == null ? undefined : String(a.sample_id),
    selected: a.selected === true,
    confidence,
    chemistry_validation: a.qa
      ? {
          status: a.qa.status ?? a.qa.state ?? "reported",
          message: a.qa.message ?? (a.qa.issues ?? []).join(" · "),
        }
      : undefined,
    provenance: {
      ...a.provenance,
      ...(a.confidence ? { confidence_evidence: a.confidence } : {}),
      ...(a.qa ? { chemistry_evidence: a.qa } : {}),
      ...(a.sample_id == null ? {} : { sample_id: a.sample_id }),
      selected: a.selected === true,
    },
  };
}
export function mapJob(j: Raw): RunJob {
  return {
    id: j.job_id,
    model: j.model,
    name: j.input_name,
    status: state(j.state),
    phase: j.phase,
    message: j.progress?.message,
    error:
      typeof j.error === "string"
        ? j.error
        : j.error
          ? JSON.stringify(j.error)
          : j.state === "interrupted"
            ? "Execution was interrupted. Review the retained evidence before making a new submission."
            : undefined,
    artifacts: (j.artifacts ?? []).map(mapArtifact),
    provenance: j.provenance,
  };
}
export function mapBatch(b: Raw): Run {
  return {
    id: b.batch_id,
    name: b.name,
    mode: b.mode,
    status: state(b.state),
    created_at: b.created_at,
    updated_at: b.updated_at,
    inputs: b.inputs,
    jobs: (b.jobs ?? [])
      .filter((j: unknown) => typeof j === "object" && j)
      .map(mapJob),
    pairs: (b.pairs ?? []).map((p: Raw) => ({
      id: p.pair_id,
      input_name: p.input_name,
      model: p.model,
      state: p.state,
      reasons: p.reasons ?? [],
      job_id: p.job_id,
    })),
    error:
      (b.errors ?? [])
        .map((e: unknown) => (typeof e === "string" ? e : JSON.stringify(e)))
        .join("\n") || undefined,
  };
}
export class WorkbenchApi {
  private token = "";
  private annotationState = new Map<string, Map<string, Raw>>();
  private annotationLoads = new Map<string, number>();
  async session(): Promise<Session> {
    const result = await this.http("/api/v1/session");
    this.token = result.csrf_token;
    return result;
  }
  private async http(path: string, init?: RequestInit): Promise<any> {
    const response = await fetch(path, {
      credentials: "same-origin",
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(init?.method && init.method !== "GET"
          ? { "X-Bio-Workbench-Token": this.token }
          : {}),
        ...init?.headers,
      },
    });
    let data: any;
    try {
      data = await response.json();
    } catch {
      throw new ApiError(
        `The local app returned an unreadable response (${response.status}).`,
      );
    }
    if (!response.ok)
      throw new ApiError(
        data.error?.message ??
          data.message ??
          `Request failed (${response.status}).`,
        data.error?.code,
      );
    return data;
  }
  async rpc<T = Raw>(method: string, params: Raw = {}): Promise<T> {
    const id = crypto.randomUUID();
    const response = await this.http("/api/v1/rpc", {
      method: "POST",
      body: JSON.stringify({ id, method, params }),
    });
    if (response.id !== id)
      throw new ApiError("RPC response identity mismatch.", "integrity");
    if (response.error)
      throw new ApiError(response.error.message, response.error.code);
    return response.result as T;
  }
  async catalog(): Promise<Catalog> {
    const c = await this.rpc("catalog");
    return {
      models: c.models.map((m: Raw) => ({
        ...m,
        output_kind: ["folding", "backbone-design"].includes(m.workflow)
          ? "structure"
          : m.workflow === "sequence-design"
            ? "sequence"
            : m.workflow === "embedding"
              ? "embedding"
              : "table",
      })),
      msa_backends: c.msa_backends,
      max_upload_bytes: c.limits?.upload_bytes,
    };
  }
  async runs(): Promise<Run[]> {
    const rows: Run[] = [];
    let cursor: string | undefined;
    const seen = new Set<string>();
    do {
      const r = await this.rpc("batch.list", {
        limit: 100,
        ...(cursor ? { cursor } : {}),
      });
      rows.push(...r.batches.map(mapBatch));
      cursor = r.next_cursor;
      if (cursor && seen.has(cursor))
        throw new ApiError(
          "Run history pagination repeated a cursor.",
          "integrity",
        );
      if (cursor) seen.add(cursor);
    } while (cursor);
    return rows;
  }
  async run(id: string): Promise<Run> {
    const b = await this.rpc("batch.get", { batch_id: id });
    const mapped = mapBatch(b);
    mapped.jobs = await Promise.all(
      (b.jobs ?? []).map(async (value: Raw | string) => {
        const j =
          typeof value === "string"
            ? await this.rpc("job.get", { job_id: value })
            : value;
        if (j.artifact_count > (j.artifacts?.length ?? 0)) {
          const all: Raw[] = [];
          let cursor: string | undefined;
          const seen = new Set<string>();
          do {
            const page = await this.rpc("job.artifacts", {
              job_id: j.job_id,
              limit: 100,
              ...(cursor ? { cursor } : {}),
            });
            all.push(...page.artifacts);
            cursor = page.next_cursor;
            if (cursor && seen.has(cursor))
              throw new ApiError(
                "Artifact pagination repeated a cursor.",
                "integrity",
              );
            if (cursor) seen.add(cursor);
          } while (cursor);
          j.artifacts = all;
        }
        return mapJob(j);
      }),
    );
    return mapped;
  }
  async preview(request: CreateRun): Promise<Run> {
    return mapBatch(
      await this.rpc("batch.validate", {
        ...request,
        inputs: request.inputs.map(
          ({ id, name, molecule_type, chain_id, source }) => ({
            id,
            name,
            molecule_type,
            ...(request.mode === "assembly" ? { chain_id } : {}),
            source,
          }),
        ),
      }),
    );
  }
  async submit(
    batchId: string,
    requestKey: string,
    pairIds: string[],
  ): Promise<Run> {
    return mapBatch(
      await this.rpc("batch.create", {
        batch_id: batchId,
        request_key: requestKey,
        pair_ids: pairIds,
      }),
    );
  }
  async cancel(id: string): Promise<Run> {
    return mapBatch(await this.rpc("batch.cancel", { batch_id: id }));
  }
  async logs(id: string): Promise<string> {
    return (
      await this.rpc("job.logs", { job_id: id, offset: 0, max_bytes: 65536 })
    ).text;
  }
  async upload(
    file: File,
    progress: (value: number) => void,
  ): Promise<UploadReceipt> {
    if (file.size > 256 * 1024 * 1024)
      throw new ApiError("Each upload is limited to 256 MB.", "limit");
    const bytes = await file.arrayBuffer();
    const hash = Array.from(
      new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
      (b) => b.toString(16).padStart(2, "0"),
    ).join("");
    const start = await this.rpc("upload.begin", {
      name: file.name,
      size: file.size,
      sha256: hash,
    });
    let offset = 0;
    while (offset < bytes.byteLength) {
      const chunk = new Uint8Array(
        bytes.slice(
          offset,
          offset + Math.min(start.chunk_bytes ?? 524288, 524288),
        ),
      );
      let binary = "";
      for (let i = 0; i < chunk.length; i += 8192)
        binary += String.fromCharCode(...chunk.subarray(i, i + 8192));
      const next = await this.rpc("upload.chunk", {
        upload_id: start.upload_id,
        offset,
        data_base64: btoa(binary),
      });
      if (next.offset !== offset + chunk.length)
        throw new ApiError("Upload offset mismatch.", "integrity");
      offset = next.offset;
      progress(bytes.byteLength ? (offset / bytes.byteLength) * 100 : 100);
    }
    const receipt = await this.rpc("upload.finish", {
      upload_id: start.upload_id,
      sha256: hash,
    });
    if (receipt.sha256 !== hash || receipt.size !== file.size)
      throw new ApiError("Upload checksum or size mismatch.", "integrity");
    return {
      upload_id: receipt.upload_id,
      name: receipt.name,
      bytes: receipt.size,
      sha256: receipt.sha256,
      format: receipt.format as InputFormat,
    };
  }
  async updateConnection(
    connection: Omit<Connection, "configured">,
  ): Promise<Session["connection"]> {
    const r = await this.http("/api/v1/connection", {
      method: "PATCH",
      body: JSON.stringify({
        ...connection,
        key_path: connection.key_path ?? "",
      }),
    });
    return r.connection ?? r;
  }
  async checkConnection(): Promise<Raw> {
    return this.http("/api/v1/connection/check", {
      method: "POST",
      body: "{}",
    });
  }
  async loadAnnotations(artifact: Artifact): Promise<AnnotationHeadState> {
    const generation = (this.annotationLoads.get(artifact.id) ?? 0) + 1;
    this.annotationLoads.set(artifact.id, generation);
    this.annotationState.delete(artifact.id);
    const result = await this.rpc("annotation.list", {
      artifact_id: artifact.id,
    });
    const known = new Map<string, Raw>();
    const mapped: Annotation[] = [];
    const deletedIds: string[] = [];
    for (const raw of result.annotations ?? []) {
      const s = raw.selection;
      if (
        s?.kind !== "bio-workbench-residue-v1" ||
        s.artifact_sha256 !== artifact.sha256
      )
        continue;
      known.set(s.local_id, raw);
      if (s.deleted) deletedIds.push(s.local_id);
      if (!s.deleted)
        mapped.push({
          id: s.local_id,
          artifact_sha256: s.artifact_sha256,
          selection: s.residue,
          label: s.label,
          note: s.note,
          color: s.color,
          created_at: raw.created_at,
        });
    }
    parseAnnotations(
      {
        schema: 1,
        kind: "bio-workbench-annotations",
        artifact_sha256: artifact.sha256,
        annotations: mapped,
        pending_deletions: deletedIds,
      },
      artifact.sha256,
    );
    if (this.annotationLoads.get(artifact.id) !== generation)
      throw new ApiError(
        "A newer annotation view superseded this response.",
        "superseded",
      );
    this.annotationState.set(artifact.id, known);
    return { annotations: mapped, deleted_ids: deletedIds };
  }
  async loadSharedNotes(artifact: Artifact): Promise<SharedNote[]> {
    const result = await this.rpc("annotation.list", {
      artifact_id: artifact.id,
    });
    return (result.annotations ?? [])
      .filter((a: Raw) => a.selection?.kind !== "bio-workbench-residue-v1")
      .map((a: Raw) => ({
        id: a.annotation_id,
        text: a.text,
        author: a.author,
        updated_at: a.updated_at,
        selection: a.selection,
      }));
  }
  async saveAnnotations(
    artifact: Artifact,
    annotations: Annotation[],
    deletedIds: string[] = [],
  ): Promise<void> {
    const known =
      this.annotationState.get(artifact.id) ?? new Map<string, Raw>();
    const unobserved = [...annotations.map((a) => a.id), ...deletedIds].filter(
      (id) => !known.has(id),
    );
    if (unobserved.length) {
      // A collision check is not a viewed revision baseline. In particular,
      // syncing a new offline note must never authorize a stale existing edit.
      const current = await this.rpc("annotation.list", {
        artifact_id: artifact.id,
      });
      const remoteIds = new Set(
        (current.annotations ?? [])
          .filter(
            (row: Raw) =>
              row.selection?.kind === "bio-workbench-residue-v1" &&
              row.selection.artifact_sha256 === artifact.sha256,
          )
          .map((row: Raw) => row.selection.local_id),
      );
      if (unobserved.some((id) => remoteIds.has(id)))
        throw new ApiError(
          "The head has annotations that this view has not reviewed. Reopen this structure to merge them before applying local edits or deletions.",
          "conflict",
        );
    }
    this.annotationState.set(artifact.id, known);
    const save = async (id: string, value: Raw, text: string) => {
      const old = known.get(id);
      if (
        old &&
        JSON.stringify(old.selection) === JSON.stringify(value) &&
        old.text === text
      )
        return;
      const result = await this.rpc("annotation.put", {
        artifact_id: artifact.id,
        ...(old
          ? {
              annotation_id: old.annotation_id,
              expected_revision: old.revision,
            }
          : {}),
        text,
        selection: value,
      });
      known.set(id, result);
    };
    for (const a of annotations)
      await save(
        a.id,
        {
          kind: "bio-workbench-residue-v1",
          local_id: a.id,
          artifact_sha256: artifact.sha256,
          residue: a.selection,
          label: a.label,
          note: a.note,
          color: a.color,
          deleted: false,
        },
        `${a.label}${a.note ? `\n\n${a.note}` : ""}`,
      );
    for (const id of deletedIds) {
      const old = known.get(id);
      if (old && !old.selection.deleted)
        await save(id, { ...old.selection, deleted: true }, old.text);
    }
  }
}
