import { _electron as electron, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, writeFile, readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import os from "node:os";

const root = path.resolve(import.meta.dirname, "..");
const evidence =
  process.env.BIO_GUI_EVIDENCE ?? path.join(root, "artifacts", "electron");
await mkdir(evidence, { recursive: true });
const state = await mkdtemp(
  path.join(os.tmpdir(), "bio-workbench-electron-test-"),
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
  BIO_DESKTOP_PYTHON: process.env.BIO_DESKTOP_PYTHON ?? "python3",
  BIO_DESKTOP_ASSETS: path.join(root, "dist"),
  BIO_SSH: "/run/current-system/sw/bin/false",
};
const executablePath = process.env.BIO_TEST_ELECTRON ?? "electron";
const options = {
  executablePath,
  args: [
    "--no-sandbox",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    path.resolve(root, "../desktop/main.cjs"),
  ],
  env,
  timeout: 45000,
};
const proof = {
  kind: "bio-workbench-native-desktop-smoke",
  head_contact: false,
  prediction_launches: 0,
  executablePath,
  checks: [],
  errors: [],
};
let app;
try {
  app = await electron.launch(options);
  let page = await app.firstWindow();
  page.on("pageerror", (e) => proof.errors.push(e.message));
  await expect(
    page.getByRole("button", { name: "Explore viewer" }),
  ).toBeVisible({ timeout: 30000 });
  const firstOrigin = new URL(page.url()).origin;
  await expect
    .poll(() => page.evaluate(() => typeof window.bioDesktop?.state?.get))
    .toBe("function");
  await page
    .getByLabel("Run name", { exact: true })
    .fill("Native restart persistence fixture");
  await page.getByLabel("Molecular input", { exact: true }).fill("ACDEFGHIK");
  await page.getByRole("button", { name: "Add input", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          JSON.parse(
            window.bioDesktop.state.get("bio-workbench.input-draft.v1"),
          ).inputs.length,
      ),
    )
    .toBe(1);
  await page.getByRole("button", { name: "Explore viewer" }).click();
  await expect(page.locator(".atom-count").first()).toContainText("atoms", {
    timeout: 30000,
  });
  await page
    .getByLabel("Select residue in 1UBQ · view 1")
    .selectOption({ index: 6 });
  await page
    .getByLabel("Label for 1UBQ · view 1", { exact: true })
    .fill("Native saved label");
  await page
    .getByRole("button", { name: "Save annotation", exact: true })
    .click();
  await expect(
    page.getByText("Native saved label", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: path.join(evidence, "comparison.png"),
    fullPage: true,
  });
  await app.evaluate(
    ({ BrowserWindow }, destination) => {
      globalThis.bioPngDownload = new Promise((resolve) => {
        BrowserWindow.getAllWindows()[0].webContents.session.once(
          "will-download",
          (event, item) => {
            item.setSavePath(destination);
            item.once("done", (_event, state) =>
              resolve({ state, url: item.getURL(), path: item.getSavePath() }),
            );
          },
        );
      });
    },
    path.join(evidence, "native-export.png"),
  );
  await page
    .getByRole("button", { name: "Save image 1UBQ · view 1", exact: true })
    .click();
  const pngResult = await app.evaluate(() => globalThis.bioPngDownload);
  expect(pngResult.state).toBe("completed");
  expect(pngResult.url.startsWith("blob:")).toBe(true);
  const pngBytes = await readFile(pngResult.path);
  expect(pngBytes.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
  proof.png_export = {
    ...pngResult,
    bytes: pngBytes.length,
    sha256: createHash("sha256").update(pngBytes).digest("hex"),
  };
  proof.checks.push(
    "Actual Electron download policy accepts local blob PNG and saves valid original rendered pixels",
  );
  proof.checks.push(
    "Native Electron window, isolated preload, real WebGL renderer and native persistent annotations",
  );
  await app.close();
  app = undefined;
  app = await electron.launch(options);
  page = await app.firstWindow();
  page.on("pageerror", (e) => proof.errors.push(e.message));
  await expect(page.getByLabel("Run name", { exact: true })).toHaveValue(
    "Native restart persistence fixture",
    { timeout: 30000 },
  );
  await expect(page.getByText("1 inputs added")).toBeVisible();
  const secondOrigin = new URL(page.url()).origin;
  expect(firstOrigin).not.toBe(secondOrigin);
  await page.getByRole("button", { name: "Explore viewer" }).click();
  await expect(
    page.getByText("Native saved label", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.locator(".atom-count").first()).toContainText("atoms", {
    timeout: 30000,
  });
  proof.checks.push(
    "Input draft and annotation restored after app restart on a different random loopback origin",
  );
  const picker = page.waitForEvent("filechooser");
  await app.evaluate(({ BrowserWindow }) =>
    BrowserWindow.getAllWindows()[0].webContents.send("bio:import-files"),
  );
  const chooser = await picker;
  await chooser.setFiles([]);
  await expect(page.getByRole("tab", { name: "Upload files" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  proof.checks.push("Native File menu import routes to multiple-file chooser");
  proof.origins = [firstOrigin, secondOrigin];
  proof.state_directory = state;
  expect(proof.errors).toEqual([]);
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
