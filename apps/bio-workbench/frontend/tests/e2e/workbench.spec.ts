import { expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const fixture = readFileSync(
  new URL("../../public/fixtures/1ubq.cif", import.meta.url),
);
const hash = createHash("sha256").update(fixture).digest("hex");
const catalog = {
  models: [
    {
      id: "rf3",
      name: "RoseTTAFold3",
      workflow: "folding",
      enabled: true,
      molecule_types: ["protein", "dna", "rna", "ligand", "assembly"],
      description: "All atom structure prediction",
      settings: {},
    },
    {
      id: "evolvepro",
      name: "EVOLVEpro",
      workflow: "variant-ranking",
      enabled: true,
      molecule_types: ["protein"],
      description: "Variant ranking",
      settings: {
        regressor: { type: "string", enum: ["rf", "xgb"], default: "rf" },
      },
    },
    {
      id: "protenix",
      name: "Protenix",
      workflow: "folding",
      enabled: true,
      molecule_types: ["protein", "dna", "rna", "ligand", "assembly"],
      description: "Biomolecular prediction",
      settings: {},
    },
    {
      id: "rfaa",
      name: "RFAA",
      enabled: false,
      disabled_reason: "Parked",
      molecule_types: ["protein"],
      description: "Parked installation",
      settings: {},
    },
  ],
  msa_backends: ["public", "private"],
};
async function mockApi(
  page: Page,
  handler: (method: string, params: any) => unknown | Promise<unknown>,
) {
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({
      json: {
        csrf_token: "test-csrf",
        connection: {
          configured: true,
          host: "test-head",
          user: "tester",
          port: 22,
          key_path: "",
        },
      },
    }),
  );
  await page.route("**/api/v1/rpc", async (route) => {
    const request = route.request().postDataJSON();
    expect(route.request().headers()["x-bio-workbench-token"]).toBe(
      "test-csrf",
    );
    try {
      const result =
        request.method === "catalog"
          ? catalog
          : await handler(request.method, request.params);
      await route.fulfill({ json: { id: request.id, result } });
    } catch (e) {
      await route.fulfill({
        json: {
          id: request.id,
          error: { code: "unavailable", message: String(e) },
        },
      });
    }
  });
}
test("native preview preserves batch input and all rejected pairs; only selected compatible pair launches", async ({
  page,
}) => {
  const calls: { method: string; params: any }[] = [];
  let batch: any = null;
  await mockApi(page, (method, params) => {
    calls.push({ method, params });
    if (method === "batch.list")
      return { batches: batch ? [batch] : [], next_cursor: null };
    if (method === "batch.validate") {
      batch = {
        batch_id: "preview-1",
        name: params.name,
        mode: params.mode,
        created_at: "2026-09-06T00:00:00Z",
        state: "validated",
        inputs: params.inputs,
        jobs: [],
        pairs: [
          {
            pair_id: "good",
            input_name: "Target",
            model: "rf3",
            state: "compatible",
            reasons: [],
          },
          {
            pair_id: "bad",
            input_name: "Target",
            model: "protenix",
            state: "rejected",
            reasons: ["Unsupported exact terminal modification"],
          },
        ],
      };
      return batch;
    }
    if (method === "batch.get") return batch;
    if (method === "batch.create") {
      batch = {
        ...batch,
        state: "queued",
        jobs: [
          {
            job_id: "job-1",
            input_name: "Target",
            model: "rf3",
            state: "queued",
            artifacts: [],
            progress: { message: "Waiting for compatible worker" },
          },
        ],
      };
      return batch;
    }
    if (method === "batch.cancel") {
      batch = { ...batch, state: "cancel_requested" };
      return batch;
    }
    throw new Error(method);
  });
  await page.goto("/");
  await expect(page.getByText("test-head", { exact: true })).toBeVisible();
  await page
    .getByLabel("Molecular input", { exact: true })
    .fill(">target\nACDEFGHIK\n");
  await page.getByRole("button", { name: "Add input", exact: true }).click();
  await page.getByRole("checkbox", { name: /RoseTTAFold3/ }).check();
  await page.getByRole("checkbox", { name: /^Protenix/ }).check();
  await page.getByRole("button", { name: "Check compatibility" }).click();
  await expect(
    page.getByText("Unsupported exact terminal modification"),
  ).toBeVisible();
  const request = calls.find((c) => c.method === "batch.validate")!.params;
  expect(request.mode).toBe("batch");
  expect(request.msa_backend).toBe("public");
  expect(request.inputs[0].source.text).toBe(">target\nACDEFGHIK\n");
  expect(request.inputs[0].source.format).toBe("fasta");
  expect(request.inputs[0]).not.toHaveProperty("bytes");
  expect(calls.some((c) => c.method === "batch.create")).toBe(false);
  await page
    .getByRole("button", { name: "Launch 1 prediction", exact: true })
    .click();
  await expect(page.getByText("Waiting for compatible worker")).toBeVisible();
  expect(
    calls.find((c) => c.method === "batch.create")!.params.pair_ids,
  ).toEqual(["good"]);
  await page.getByRole("button", { name: "Cancel run", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Cancellation requested" }),
  ).toBeVisible();
  expect(calls.filter((c) => c.method === "batch.create")).toHaveLength(1);
});
test("many-file upload preserves exact bytes and chunk hashes; assembly components remain explicit", async ({
  page,
}) => {
  const uploads = new Map<
    string,
    { name: string; size: number; bytes: Buffer }
  >();
  await mockApi(page, (method, params) => {
    if (method === "batch.list") return { batches: [] };
    if (method === "upload.begin") {
      const id = `u${uploads.size}`;
      uploads.set(id, {
        name: params.name,
        size: params.size,
        bytes: Buffer.alloc(0),
      });
      return { upload_id: id, chunk_bytes: 8 };
    }
    if (method === "upload.chunk") {
      const u = uploads.get(params.upload_id)!;
      expect(params.offset).toBe(u.bytes.length);
      u.bytes = Buffer.concat([
        u.bytes,
        Buffer.from(params.data_base64, "base64"),
      ]);
      return { offset: u.bytes.length };
    }
    if (method === "upload.finish") {
      const u = uploads.get(params.upload_id)!;
      expect(createHash("sha256").update(u.bytes).digest("hex")).toBe(
        params.sha256,
      );
      expect(u.bytes.length).toBe(u.size);
      return {
        upload_id: params.upload_id,
        name: u.name,
        size: u.size,
        sha256: params.sha256,
      };
    }
    throw new Error(method);
  });
  await page.goto("/");
  await page.getByRole("tab", { name: "Upload files" }).click();
  await page.getByLabel("Choose molecular files").setInputFiles([
    {
      name: "binder.fa",
      mimeType: "text/plain",
      buffer: Buffer.from(">binder\nACDEFGHIK\n"),
    },
    {
      name: "ligand.smi",
      mimeType: "text/plain",
      buffer: Buffer.from("C[C@H](O)F\n"),
    },
  ]);
  await expect(page.getByText("2 inputs added")).toBeVisible();
  expect(uploads.size).toBe(2);
  await expect(page.getByLabel("Type for ligand.smi")).toHaveValue("ligand");
  await page.getByRole("button", { name: /One assembly/ }).click();
  await expect(page.getByLabel("Chain for binder.fa")).toHaveValue("A");
  await expect(page.getByLabel("Chain for ligand.smi")).toHaveValue("B");
});
test("failed preview retry reuses its request key without silently launching", async ({
  page,
}) => {
  const keys: string[] = [];
  await mockApi(page, (method, params) => {
    if (method === "batch.list") return { batches: [] };
    if (method === "batch.validate") {
      keys.push(params.request_key);
      throw new Error("Connection interrupted after request");
    }
    throw new Error(`Unexpected ${method}`);
  });
  await page.goto("/");
  await page.getByLabel("Molecular input", { exact: true }).fill("ACDE");
  await page.getByRole("button", { name: "Add input", exact: true }).click();
  await page.getByRole("checkbox", { name: /RoseTTAFold3/ }).check();
  await page.getByRole("button", { name: "Check compatibility" }).click();
  await expect(page.getByRole("alert")).toContainText("Connection interrupted");
  await page.getByRole("button", { name: "Check compatibility" }).click();
  await expect.poll(() => keys.length).toBe(2);
  expect(keys[0]).toBe(keys[1]);
});
test("resetting an enum to Native default removes the scientific override", async ({
  page,
}) => {
  let request: any;
  await mockApi(page, (method, params) => {
    if (method === "batch.list") return { batches: [] };
    if (method === "batch.validate") {
      request = params;
      return {
        batch_id: "enum-preview",
        name: params.name,
        mode: "batch",
        state: "validating",
        jobs: [],
        pairs: [],
        created_at: "2026-09-06T00:00:00Z",
      };
    }
    if (method === "batch.get")
      return {
        batch_id: "enum-preview",
        state: "validating",
        jobs: [],
        pairs: [],
        created_at: "2026-09-06T00:00:00Z",
      };
    throw new Error(method);
  });
  await page.goto("/");
  await page.getByLabel("Molecular input", { exact: true }).fill("ACDEFGHIK");
  await page.getByRole("button", { name: "Add input", exact: true }).click();
  await page.getByRole("checkbox", { name: /^EVOLVEpro/ }).check();
  await page.getByText("Run settings", { exact: true }).click();
  await page
    .getByLabel("EVOLVEpro · regressor", { exact: true })
    .selectOption("xgb");
  await page
    .getByLabel("EVOLVEpro · regressor", { exact: true })
    .selectOption("");
  await page
    .getByRole("button", { name: "Check compatibility", exact: true })
    .click();
  await expect.poll(() => request).toBeTruthy();
  expect(request.settings.evolvepro).toEqual({});
});
test("actual WebGL structure views render, annotation binds to hash and survives remount, export works", async ({
  page,
}) => {
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({
      json: {
        csrf_token: "offline",
        connection: {
          configured: false,
          host: "",
          user: "root",
          port: 22,
          key_path: "",
        },
      },
    }),
  );
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page.getByRole("button", { name: "Explore viewer" }).click();
  await expect(page.locator(".atom-count").first()).toContainText("atoms", {
    timeout: 30000,
  });
  await expect(page.locator(".molecule-canvas canvas")).toHaveCount(2);
  await expect(page.getByText("By pLDDT", { exact: true })).toHaveCount(0);
  const select = page.getByLabel("Select residue in 1UBQ · view 1");
  await select.selectOption({ index: 5 });
  await page
    .getByLabel("Label for 1UBQ · view 1", { exact: true })
    .fill("Binding site hypothesis");
  await page
    .getByLabel("Annotation note for 1UBQ · view 1")
    .fill("Check with a binding assay.");
  await page
    .getByRole("button", { name: "Save annotation", exact: true })
    .click();
  await expect(
    page.getByText("Check with a binding assay.", { exact: true }),
  ).toBeVisible();
  const stored = await page.evaluate(
    (hash) =>
      JSON.parse(localStorage.getItem(`bio-workbench.annotations.v1.${hash}`)!),
    hash,
  );
  expect(stored.artifact_sha256).toBe(hash);
  expect(stored.annotations[0].label).toBe("Binding site hypothesis");
  const download = page.waitForEvent("download");
  await page
    .getByRole("button", {
      name: "Export annotations 1UBQ · view 1",
      exact: true,
    })
    .click();
  expect((await download).suggestedFilename()).toContain(hash.slice(0, 12));
  const png = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Save image 1UBQ · view 1", exact: true })
    .click();
  expect((await png).suggestedFilename()).toContain(".png");
  await page
    .getByRole("button", { name: "Prepare inputs", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Compare structures", exact: true })
    .click();
  await expect(
    page.getByText("Binding site hypothesis", { exact: true }).first(),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/desktop-renderer-comparison.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
test("mismatched structure SHA refuses rendering; shared Slack notes are plain text", async ({
  page,
}) => {
  const artifact = {
    artifact_id: "wrong-hash",
    name: "bad.cif",
    format: "mmcif",
    role: "structure",
    sha256: "a".repeat(64),
    size: fixture.length,
    model: "rf3",
    confidence: null,
    qa: null,
  };
  const batch = {
    batch_id: "batch",
    name: "Integrity check",
    state: "complete",
    mode: "batch",
    created_at: "2026-09-06T00:00:00Z",
    jobs: [
      {
        job_id: "job",
        model: "rf3",
        state: "complete",
        input_name: "test",
        artifacts: [artifact],
      },
    ],
  };
  await mockApi(page, (method) => {
    if (method === "batch.list") return { batches: [batch] };
    if (method === "batch.get") return batch;
    if (method === "annotation.list")
      return {
        annotations: [
          {
            annotation_id: "note",
            text: "Harrison: check activity <script>alert(1)</script>",
            selection: null,
            author: "harrison",
            updated_at: "2026-09-06T00:00:00Z",
          },
        ],
      };
    throw new Error(method);
  });
  await page.route("**/api/v1/artifacts/wrong-hash", (route) =>
    route.fulfill({ body: fixture, contentType: "text/plain" }),
  );
  await page.goto("/?batch=batch");
  await page.getByRole("button", { name: "View 3D" }).click();
  await expect(
    page.getByText("Structure checksum mismatch. The file was not displayed."),
  ).toBeVisible();
  await expect(
    page.getByText("Harrison: check activity <script>alert(1)</script>", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.locator(".molecule-canvas canvas")).toHaveCount(0);
});
test("compact window keeps input controls within viewport and has explicit grouping", async ({
  page,
}) => {
  await page.setViewportSize({ width: 430, height: 900 });
  await mockApi(page, (method) => {
    if (method === "batch.list") return { batches: [] };
    throw new Error(method);
  });
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: /Separate predictions/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /One assembly/ }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/desktop-renderer-compact.png",
    fullPage: true,
  });
});
for (const priorCopies of [0, 100])
  test(`reopening a two-device annotation conflict preserves ${priorCopies} prior copies and never deletes unseen remote notes`, async ({
    page,
  }) => {
    const localNote = (id: string, label: string, note: string) => ({
      id,
      artifact_sha256: hash,
      selection: { chain: "A", resi: 4, atom: "CA" },
      label,
      note,
      color: "#e79b41",
      created_at: "2026-09-06T00:00:00Z",
    });
    const old = localNote("same", "Shared site", "Offline conflicting edit");
    const local = {
      schema: 1,
      kind: "bio-workbench-annotations",
      artifact_sha256: hash,
      annotations: [
        old,
        localNote("local-new", "Local addition", "Not uploaded yet"),
        localNote("removed", "Deleted elsewhere", "Old local copy"),
      ],
      pending_deletions: ["remote-new"],
      local_conflict_copies: Array.from({ length: priorCopies }, () => ({
        annotations: [old],
        pending_deletions: [],
        captured_at: "2026-09-06T00:00:00Z",
      })),
    };
    await page.addInitScript(
      ({ hash, local }) =>
        localStorage.setItem(
          `bio-workbench.annotations.v1.${hash}`,
          JSON.stringify(local),
        ),
      { hash, local },
    );
    const raw = (note: ReturnType<typeof localNote>, deleted = false) => ({
      annotation_id: `head-${note.id}`,
      artifact_id: "shared-artifact",
      revision: 3,
      text: `${note.label}\n\n${note.note}`,
      selection: {
        kind: "bio-workbench-residue-v1",
        local_id: note.id,
        artifact_sha256: hash,
        residue: note.selection,
        label: note.label,
        note: note.note,
        color: note.color,
        deleted,
      },
      created_at: note.created_at,
    });
    const head = new Map(
      [
        raw(localNote("same", "Shared site", "Current head edit")),
        raw(localNote("remote-new", "Remote new", "Written on another device")),
        raw(
          localNote(
            "removed",
            "Deleted elsewhere",
            "Deleted by another device",
          ),
          true,
        ),
      ].map((n) => [n.annotation_id, n]),
    );
    const writes: any[] = [];
    let rejectDeletion = false;
    const artifact = {
      artifact_id: "shared-artifact",
      name: "shared.cif",
      format: "mmcif",
      role: "structure",
      sha256: hash,
      size: fixture.length,
      model: "rf3",
      confidence: { metrics: { has_clash: false, plddt: 91.23456 } },
    };
    const batch = {
      batch_id: "shared-batch",
      name: "Shared notes",
      state: "complete",
      mode: "batch",
      created_at: "2026-09-06T00:00:00Z",
      jobs: [
        {
          job_id: "shared-job",
          model: "rf3",
          state: "complete",
          input_name: "test",
          artifacts: [artifact],
        },
      ],
    };
    await mockApi(page, (method, params) => {
      if (method === "batch.list") return { batches: [batch] };
      if (method === "batch.get") return batch;
      if (method === "annotation.list")
        return { annotations: [...head.values()] };
      if (method === "annotation.put") {
        if (rejectDeletion && params.selection?.deleted)
          throw new Error("offline deletion test");
        writes.push(params);
        const previous = params.annotation_id
          ? head.get(params.annotation_id)
          : undefined;
        if (previous && previous.revision !== params.expected_revision)
          throw new Error("conflict");
        const result = {
          ...params,
          annotation_id:
            previous?.annotation_id ?? `head-${params.selection.local_id}`,
          revision: (previous?.revision ?? 0) + 1,
          created_at: "2026-09-06T00:00:00Z",
        };
        head.set(result.annotation_id, result);
        return result;
      }
      throw new Error(method);
    });
    await page.route("**/api/v1/artifacts/shared-artifact", (route) =>
      route.fulfill({ body: fixture, contentType: "text/plain" }),
    );
    await page.goto("/?batch=shared-batch");
    await page.getByRole("button", { name: "View 3D" }).click();
    await expect(
      page.getByText("Current head edit", { exact: true }),
    ).toBeVisible();
    await expect(
      page.locator(".native-metrics").getByText("false", { exact: true }),
    ).toBeVisible();
    await expect(
      page.locator(".native-metrics").getByText("91.235", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Written on another device", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Local addition", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Deleted elsewhere", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", {
        name: `Export preserved local copies (${priorCopies + 1})`,
        exact: true,
      }),
    ).toBeVisible();
    expect(writes).toHaveLength(0);
    const preserved = await page.evaluate(
      (hash) =>
        JSON.parse(
          localStorage.getItem(`bio-workbench.annotations.v1.${hash}`)!,
        ),
      hash,
    );
    if (priorCopies === 100) {
      // Never replace the valid old document with an unreadable 101-copy one.
      expect(preserved).toEqual(local);
      await expect(
        page.getByText(/previous local copy was not overwritten/),
      ).toBeVisible();
    } else {
      expect(preserved.local_conflict_copies[0].annotations[0].note).toBe(
        "Offline conflicting edit",
      );
      expect(preserved.local_conflict_copies[0].pending_deletions).toEqual([
        "remote-new",
      ]);
    }
    await expect(page.locator(".atom-count")).toContainText("atoms");
    await page
      .getByLabel("Select residue in shared.cif")
      .selectOption({ index: 6 });
    await page
      .getByLabel("Label for shared.cif", { exact: true })
      .fill("New explicit note");
    await page
      .getByRole("button", { name: "Save annotation", exact: true })
      .click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).not.toHaveProperty("annotation_id");
    expect(head.get("head-same")!.selection.note).toBe("Current head edit");
    expect(head.get("head-remote-new")!.selection.deleted).toBe(false);
    await page
      .getByRole("button", {
        name: "Delete annotation Remote new",
        exact: true,
      })
      .click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1].annotation_id).toBe("head-remote-new");
    expect(writes[1].selection.deleted).toBe(true);
    expect(head.get("head-same")!.selection.note).toBe("Current head edit");
    if (priorCopies === 0) {
      rejectDeletion = true;
      await page
        .getByRole("button", {
          name: "Delete annotation Shared site",
          exact: true,
        })
        .click();
      await expect(
        page.getByText(/1 local deletions are not confirmed/),
      ).toBeVisible();
      await expect(page.getByText(/head sync failed/)).toBeVisible();
      await page
        .getByLabel("Label for shared.cif", { exact: true })
        .fill("Unrelated synced addition");
      await page
        .getByRole("button", { name: "Save annotation", exact: true })
        .click();
      await expect(
        page.getByText(
          /This change synced; other local additions or deletions still need review/,
        ),
      ).toBeVisible();
      await expect(
        page.getByText(/1 local deletions are not confirmed/),
      ).toBeVisible();
      const downloadEvent = page.waitForEvent("download");
      await page
        .getByLabel("Export annotations shared.cif", { exact: true })
        .click();
      const downloaded = await downloadEvent;
      const exported = JSON.parse(
        readFileSync((await downloaded.path())!, "utf8"),
      );
      expect(exported.pending_deletions).toEqual(["same"]);
      expect(exported.local_conflict_copies).toHaveLength(1);
      expect(head.get("head-same")!.selection.note).toBe("Current head edit");
    }
  });
