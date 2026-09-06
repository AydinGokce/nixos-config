// Explicit integration test: real head CPU validation only. The route guard
// refuses every inference commit even if a future UI change attempts one.
import { _electron as electron, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
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
    "Set explicit BIO_GUI_EVIDENCE, BIO_TEST_ELECTRON and BIO_DESKTOP_PYTHON before running the authorized head CPU preview.",
  );
await mkdir(evidence, { recursive: true });
const state = await mkdtemp(
  path.join(os.tmpdir(), "bio-workbench-head-preview-"),
);
const xvfb = spawn(
  "Xvfb",
  ["-displayfd", "1", "-screen", "0", "1500x1000x24", "-nolisten", "tcp"],
  { stdio: ["ignore", "pipe", "pipe"] },
);
const display = await new Promise((resolve, reject) => {
  xvfb.stdout.once("data", (bytes) => resolve(`:${bytes.toString().trim()}`));
  xvfb.once("error", reject);
});
const env = {
  ...process.env,
  DISPLAY: display,
  XDG_CONFIG_HOME: path.join(state, "config"),
  XDG_CACHE_HOME: path.join(state, "cache"),
  BIO_DESKTOP_ASSETS: path.join(root, "dist"),
};
delete env.BIO_SSH;
const proof = {
  kind: "bio-workbench-real-head-native-desktop-cpu-preview",
  status: "running",
  state_directory: state,
  inference_commit_attempts: 0,
  requests: [],
  responses: [],
  errors: [],
};
let app;
try {
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
    proof.requests.push({
      id: request.id,
      method: request.method,
      params: request.params,
    });
    if (request.method === "batch.create") {
      proof.inference_commit_attempts++;
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
  page.on("response", async (response) => {
    if (response.url().endsWith("/api/v1/rpc")) {
      try {
        proof.responses.push(await response.json());
      } catch {}
    }
  });
  await expect(
    page.getByRole("button", { name: "31.56.109.100", exact: true }),
  ).toBeVisible({ timeout: 60000 });
  await expect(
    page.getByRole("checkbox", { name: /^RoseTTAFold3/ }),
  ).toBeVisible();
  await page
    .getByLabel("Run name", { exact: true })
    .fill("Desktop integration — CPU preview only");
  await page.getByRole("tab", { name: "Upload files" }).click();
  const picker = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: "Browse files", exact: true }).click();
  const chooser = await picker;
  await chooser.setFiles({
    name: "1ubq-desktop-preview.fasta",
    mimeType: "text/plain",
    buffer: Buffer.from(
      ">1ubq_A\nMQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG\n",
    ),
  });
  await expect(page.getByText("1 inputs added", { exact: true })).toBeVisible({
    timeout: 60000,
  });
  for (const name of [/^Boltz 2/, /^Protenix/, /^OpenFold3/, /^RoseTTAFold3/])
    await page.getByRole("checkbox", { name }).check();
  await page
    .getByRole("button", { name: "Public service", exact: true })
    .click();
  await page.screenshot({
    path: path.join(evidence, "inputs.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Check compatibility", exact: true })
    .click();
  await expect
    .poll(
      () =>
        proof.responses
          .map((r) => r.result)
          .findLast(
            (r) =>
              r?.name === "Desktop integration — CPU preview only" &&
              ["validated", "validation_failed"].includes(r.state),
          ),
      { timeout: 240000, intervals: [1000] },
    )
    .toBeTruthy();
  const batch = proof.responses
    .map((r) => r.result)
    .findLast(
      (r) =>
        r?.name === "Desktop integration — CPU preview only" &&
        ["validated", "validation_failed"].includes(r.state),
    );
  proof.batch = batch;
  await page.screenshot({
    path: path.join(evidence, "validated.png"),
    fullPage: true,
  });
  expect(batch.state).toBe("validated");
  expect(batch.pairs).toHaveLength(4);
  expect(batch.pairs.every((pair) => pair.state === "compatible")).toBe(true);
  expect(batch.jobs).toHaveLength(0);
  expect(proof.inference_commit_attempts).toBe(0);
  const request = proof.requests.find((r) => r.method === "batch.validate");
  expect(request.params.msa_backend).toBe("public");
  expect(request.params.mode).toBe("batch");
  expect(
    proof.requests.filter((r) => r.method === "upload.finish"),
  ).toHaveLength(1);
  proof.preview_only = true;
  proof.status = "passed";
} catch (error) {
  proof.status = "failed";
  proof.error = String(error.stack ?? error);
  throw error;
} finally {
  if (app) await app.close();
  xvfb.kill("SIGTERM");
  await writeFile(
    path.join(evidence, "proof.json"),
    JSON.stringify(proof, null, 2) + "\n",
  );
}
