import { useState } from "react";
import { safeArtifactUrl } from "../domain";
import type { Artifact } from "../types";
export function parseTable(text: string, delimiter = ","): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') {
      if (quoted && text[i + 1] === '"') {
        field += '"';
        i++;
      } else quoted = !quoted;
    } else if (c === delimiter && !quoted) {
      row.push(field);
      field = "";
    } else if (c === "\n" && !quoted) {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else field += c;
  }
  if (field.length || row.length) {
    row.push(field.replace(/\r$/, ""));
    rows.push(row);
  }
  return rows;
}
export function ArtifactPreview({ artifact }: { artifact: Artifact }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  if (
    !["csv", "tsv", "json", "fasta", "fa", "txt", "log"].includes(
      artifact.format.toLowerCase(),
    ) ||
    (artifact.bytes ?? 0) > 2 * 1024 * 1024
  )
    return null;
  async function toggle() {
    setOpen(!open);
    if (text !== null || open) return;
    try {
      const response = await fetch(safeArtifactUrl(artifact.url), {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`Download failed (${response.status})`);
      const buffer = await response.arrayBuffer();
      if (buffer.byteLength > 2 * 1024 * 1024)
        throw new Error("Preview limit is 2 MB; use the original download.");
      const digest = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", buffer)),
        (b) => b.toString(16).padStart(2, "0"),
      ).join("");
      if (digest !== artifact.sha256)
        throw new Error("Result checksum mismatch.");
      setText(new TextDecoder().decode(buffer));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const table =
    text !== null && ["csv", "tsv"].includes(artifact.format)
      ? parseTable(text, artifact.format === "tsv" ? "\t" : ",")
      : null;
  return (
    <div className="artifact-preview">
      <button className="text-link" onClick={() => void toggle()}>
        {open ? "Hide preview" : "Preview result"}
      </button>
      {open &&
        (error ? (
          <p className="inline-error">{error}</p>
        ) : text === null ? (
          <p className="empty-note">Verifying result…</p>
        ) : table ? (
          <>
            <div className="data-table-wrap">
              <table>
                <thead>
                  <tr>
                    {table[0]?.slice(0, 60).map((value, i) => (
                      <th key={i}>{value}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.slice(1, 101).map((row, i) => (
                    <tr key={i}>
                      {row.slice(0, 60).map((value, j) => (
                        <td key={j}>{value}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <small>
              {Math.min(100, Math.max(0, table.length - 1))} of{" "}
              {Math.max(0, table.length - 1)} rows shown · up to 60 columns.
              Original download is unchanged.
            </small>
          </>
        ) : (
          <pre>
            {text.slice(0, 100000)}
            {text.length > 100000
              ? "\n…Preview truncated; download the complete result."
              : ""}
          </pre>
        ))}
    </div>
  );
}
