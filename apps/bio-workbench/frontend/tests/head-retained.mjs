// Explicit retained-result integration. No validation, upload, inference or
// cancellation is permitted; only a labeled test annotation may be changed.
import { _electron as electron, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, writeFile, readFile } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import path from "node:path";
import os from "node:os";

const root = path.resolve(import.meta.dirname, "..");
const evidence = process.env.BIO_GUI_EVIDENCE;
if (
  !evidence ||
  !process.env.BIO_TEST_ELECTRON ||
  !process.env.BIO_DESKTOP_PYTHON
)
  throw new Error(
    "Set explicit evidence, Electron and Python paths for the authorized retained-result test.",
  );
await mkdir(evidence, { recursive: true });
const state = await mkdtemp(
  path.join(os.tmpdir(), "bio-workbench-head-retained-"),
);
const batchId = "44d03459e4844d13a4d100dd4073672f";
const jobId = "f5c4fbf1abea4a21b17cd87f102272d7";
const label = `UI integration test — ${randomUUID().slice(0, 8)}`;
const xvfb = spawn(
  "Xvfb",
  ["-displayfd", "1", "-screen", "0", "1500x1000x24", "-nolisten", "tcp"],
  { stdio: ["ignore", "pipe", "pipe"] },
);
const display = await new Promise((resolve, reject) => {
  xvfb.stdout.once("data", (bytes) => resolve(`:${bytes.toString().trim()}`));
  xvfb.once("error", reject);
});
const proof = {
  kind: "bio-workbench-real-head-retained-result-desktop-test",
  status: "running",
  batch_id: batchId,
  job_id: jobId,
  state_directory: state,
  label,
  forbidden_requests: [],
  requests: [],
  responses: [],
  errors: [],
};
const ownAnnotationIds = new Set();
const allowed = new Set([
  "catalog",
  "batch.list",
  "batch.get",
  "job.get",
  "job.artifacts",
  "job.logs",
  "annotation.list",
  "annotation.put",
]);
let app;
async function open(device) {
  const env = {
    ...process.env,
    DISPLAY: display,
    XDG_CONFIG_HOME: path.join(state, device, "config"),
    XDG_CACHE_HOME: path.join(state, device, "cache"),
    BIO_DESKTOP_ASSETS: path.join(root, "dist"),
  };
  delete env.BIO_SSH;
  app = await electron.launch({
    executablePath: process.env.BIO_TEST_ELECTRON,
    args: [
      "--no-sandbox",
      "--use-gl=angle",
      "--use-angle=swiftshader",
      "--enable-unsafe-swiftshader",
      path.resolve(root, "../desktop/main.cjs"),
    ],
    env,
    timeout: 45000,
  });
  const page = await app.firstWindow();
  page.on("pageerror", (error) => proof.errors.push(error.message));
  await page.route("**/api/v1/rpc", async (route) => {
    const request = route.request().postDataJSON();
    proof.requests.push({ device, ...request });
    if (
      !allowed.has(request.method) ||
      (request.method === "annotation.put" &&
        (request.params.selection?.label !== label ||
          (request.params.annotation_id &&
            !ownAnnotationIds.has(request.params.annotation_id))))
    ) {
      proof.forbidden_requests.push(request);
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
  page.on("response", async (response) => {
    if (response.url().endsWith("/api/v1/rpc"))
      try {
        const value = await response.json();
        proof.responses.push({ device, ...value });
        if (
          value.result?.selection?.label === label &&
          value.result?.annotation_id
        )
          ownAnnotationIds.add(value.result.annotation_id);
      } catch {}
  });
  await expect(
    page.getByRole("button", { name: "31.56.109.100", exact: true }),
  ).toBeVisible({ timeout: 60000 });
  return page;
}
async function inspectBatch(page) {
  await page.getByRole("button", { name: "Run history", exact: true }).click();
  await expect
    .poll(() => proof.requests.some((r) => r.method === "batch.get"))
    .toBe(true);
  await page
    .locator(".run-list-item")
    .filter({
      hasText: "Retained validation - Protenix ubiquitin - no new prediction",
    })
    .click();
  await expect(
    page.getByRole("button", { name: "View 3D", exact: true }),
  ).toHaveCount(5, { timeout: 60000 });
  const rows = proof.responses
    .flatMap((r) => r.result?.artifacts ?? [])
    .filter((a) => a.job_id === jobId && a.role === "structure");
  const structures = [...new Map(rows.map((a) => [a.artifact_id, a])).values()];
  expect(structures).toHaveLength(5);
  expect(structures.every((a) => a.confidence?.metrics?.plddt > 0)).toBe(true);
  return structures;
}
async function download(page, selector, destination) {
  await app.evaluate(({ BrowserWindow }, destination) => {
    globalThis.bioRetainedDownload = new Promise((resolve) => {
      BrowserWindow.getAllWindows()[0].webContents.session.once(
        "will-download",
        (_event, item) => {
          item.setSavePath(destination);
          item.once("done", (_event, state) =>
            resolve({ state, url: item.getURL(), path: item.getSavePath() }),
          );
        },
      );
    });
  }, destination);
  await selector.click();
  const result = await app.evaluate(() => globalThis.bioRetainedDownload);
  expect(result.state).toBe("completed");
  const bytes = await readFile(destination);
  return {
    ...result,
    bytes: bytes.length,
    sha256: createHash("sha256").update(bytes).digest("hex"),
  };
}
try {
  let page = await open("device-one");
  proof.first_origin = new URL(page.url()).origin;
  const structures = await inspectBatch(page);
  proof.structures = structures;
  proof.first_history_hydrated = true;
  await page
    .getByRole("button", { name: "View 3D", exact: true })
    .first()
    .click();
  await expect(page.locator(".atom-count").first()).toContainText("atoms", {
    timeout: 60000,
  });
  const first = structures[0];
  let card = page.getByTestId(`structure-${first.artifact_id}`);
  await expect(card.locator(".native-metrics")).toContainText("plddt");
  await card
    .getByLabel(`Select residue in ${first.name}`)
    .selectOption({ index: 6 });
  await card.getByLabel(`Label for ${first.name}`, { exact: true }).fill(label);
  await card
    .getByLabel(`Annotation note for ${first.name}`)
    .fill(
      "UI integration test on an existing prediction. This note is not a scientific conclusion and no new prediction was run.",
    );
  await card
    .getByRole("button", { name: "Save annotation", exact: true })
    .click();
  await expect(
    card.getByText("Saved on this device and head", { exact: true }),
  ).toBeVisible({ timeout: 60000 });
  proof.annotation_created = proof.responses.findLast(
    (r) => r.result?.selection?.label === label,
  )?.result;
  expect(proof.annotation_created.selection.artifact_sha256).toBe(first.sha256);
  proof.original_download = await download(
    page,
    card.getByRole("link", { name: `Download ${first.name}`, exact: true }),
    path.join(evidence, "original-structure.cif"),
  );
  expect(proof.original_download.sha256).toBe(first.sha256);
  await page.getByRole("button", { name: "Run history", exact: true }).click();
  await page
    .getByRole("button", { name: "View 3D", exact: true })
    .nth(1)
    .click();
  await expect(page.locator(".atom-count")).toHaveCount(2);
  await expect(page.locator(".atom-count").nth(1)).toContainText("atoms", {
    timeout: 60000,
  });
  proof.atom_counts = await page.locator(".atom-count").allTextContents();
  await page.screenshot({
    path: path.join(evidence, "retained-comparison.png"),
    fullPage: true,
  });
  await app.close();
  app = undefined;
  page = await open("device-two");
  proof.second_origin = new URL(page.url()).origin;
  expect(proof.second_origin).not.toBe(proof.first_origin);
  await inspectBatch(page);
  await page
    .getByRole("button", { name: "View 3D", exact: true })
    .first()
    .click();
  card = page.getByTestId(`structure-${first.artifact_id}`);
  await expect(card.getByText(label, { exact: true })).toBeVisible({
    timeout: 60000,
  });
  proof.annotation_restored_without_local_state = true;
  await page.screenshot({
    path: path.join(evidence, "shared-annotation-second-device.png"),
    fullPage: true,
  });
  await card
    .getByRole("button", { name: `Delete annotation ${label}`, exact: true })
    .click();
  await expect(
    card.getByText("Saved on this device and head", { exact: true }),
  ).toBeVisible({ timeout: 60000 });
  proof.annotation_cleanup = proof.responses.findLast(
    (r) => r.result?.selection?.label === label,
  )?.result;
  expect(proof.annotation_cleanup.annotation_id).toBe(
    proof.annotation_created.annotation_id,
  );
  expect(proof.annotation_cleanup.selection.deleted).toBe(true);
  expect(proof.annotation_cleanup.revision).toBe(
    proof.annotation_created.revision + 1,
  );
  expect(proof.forbidden_requests).toHaveLength(0);
  expect(proof.errors).toHaveLength(0);
  proof.prediction_launches = 0;
  proof.status = "passed";
} catch (error) {
  proof.status = "failed";
  proof.error = String(error.stack ?? error);
  if (app)
    try {
      await (
        await app.firstWindow()
      ).screenshot({
        path: path.join(evidence, "failure.png"),
        fullPage: true,
      });
    } catch {}
  throw error;
} finally {
  if (app) await app.close();
  xvfb.kill("SIGTERM");
  await writeFile(
    path.join(evidence, "proof.json"),
    JSON.stringify(proof, null, 2) + "\n",
  );
}
