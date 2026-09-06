export type MoleculeType =
  | "protein"
  | "dna"
  | "rna"
  | "ligand"
  | "assembly"
  | "structure";
export type InputFormat =
  | "sequence"
  | "fasta"
  | "smiles"
  | "ccd"
  | "sdf"
  | "pdb"
  | "mmcif"
  | "library-json"
  | "contigs";
export type InputSource =
  | { kind: "text"; text: string; format: InputFormat }
  | { kind: "upload"; upload_id: string; format: InputFormat }
  | { kind: "library"; ref: string; format?: "library-json" };
export interface MolecularInput {
  id: string;
  name: string;
  molecule_type: MoleculeType;
  chain_id?: string;
  source: InputSource;
  bytes?: number;
  sha256?: string;
}
export interface ModelSpec {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  disabled_reason?: string;
  molecule_types: string[];
  modes?: ("batch" | "assembly")[];
  output_kind?: "structure" | "table" | "sequence" | "embedding" | "files";
  limitations?: string[];
  settings?: Record<
    string,
    {
      type: "integer" | "number" | "string" | "boolean";
      label?: string;
      default?: unknown;
      minimum?: number;
      maximum?: number;
      enum?: (string | number)[];
    }
  >;
}
export interface Catalog {
  models: ModelSpec[];
  connection?: { status: string; name?: string; message?: string };
  msa_backends?: string[];
  max_upload_bytes?: number;
}
export type RunStatus =
  | "validated"
  | "validation_failed"
  | "created"
  | "preparing"
  | "queued"
  | "running"
  | "validating"
  | "succeeded"
  | "partial"
  | "failed"
  | "cancelling"
  | "cancelled";
export interface Artifact {
  id: string;
  name: string;
  kind: "structure" | "table" | "sequence" | "embedding" | "file";
  format: string;
  url: string;
  sha256: string;
  bytes?: number;
  model?: string;
  sample_index?: number;
  chain_mapping?: Record<string, string>;
  sample_id?: string;
  selected?: boolean;
  confidence?: {
    kind: string;
    source: string;
    mean?: number;
    minimum?: number;
    maximum?: number;
    atom_property?: string;
    metrics?: Record<string, number | boolean>;
  };
  chemistry_validation?: { status: string; message?: string };
  provenance?: Record<string, unknown>;
  preview?: { columns: string[]; rows: (string | number | null)[][] };
}
export interface RunJob {
  id: string;
  model: string;
  name?: string;
  status: RunStatus;
  phase?: string;
  progress?: number;
  message?: string;
  error?: string;
  artifacts?: Artifact[];
  settings?: Record<string, unknown>;
  provenance?: Record<string, unknown>;
}
export interface Run {
  id: string;
  name: string;
  mode: "batch" | "assembly";
  status: RunStatus;
  pairs?: ValidationPair[];
  created_at: string;
  updated_at?: string;
  jobs: RunJob[];
  inputs?: MolecularInput[];
  events?: { timestamp: string; message: string; level?: string }[];
  error?: string;
  request_key?: string;
}
export interface ValidationPair {
  id: string;
  input_name: string;
  model: string;
  state: string;
  reasons: string[];
  job_id?: string;
}
export interface CreateRun {
  request_key: string;
  name: string;
  mode: "batch" | "assembly";
  inputs: MolecularInput[];
  models: string[];
  msa_backend: "public" | "private";
  execution: "auto" | "resident" | "ephemeral";
  settings: Record<string, Record<string, unknown>>;
}
export interface UploadReceipt {
  upload_id: string;
  name: string;
  bytes: number;
  sha256: string;
  format?: InputFormat;
}
export interface ResidueSelection {
  chain: string;
  resi: number;
  icode?: string;
  resn?: string;
  atom?: string;
  serial?: number;
}
export interface Annotation {
  id: string;
  artifact_sha256: string;
  selection: ResidueSelection;
  label: string;
  note: string;
  color: string;
  created_at: string;
}
export interface AnnotationDocument {
  schema: 1;
  kind: "bio-workbench-annotations";
  artifact_sha256: string;
  annotations: Annotation[];
  pending_deletions?: string[];
  local_conflict_copies?: {
    annotations: Annotation[];
    pending_deletions: string[];
    captured_at: string;
  }[];
}
export interface AnnotationHeadState {
  annotations: Annotation[];
  deleted_ids: string[];
}
export interface SharedNote {
  id: string;
  text: string;
  author: string;
  updated_at: string;
  selection?: unknown;
}
