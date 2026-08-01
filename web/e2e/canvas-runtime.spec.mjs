// @ts-check
import { readFileSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { expect, test } from "@playwright/test";

const runtimePath = process.env.MEMENTO_CURSOR_CANVAS_RUNTIME;
const typescriptPath = process.env.MEMENTO_CURSOR_TYPESCRIPT;

test("Canvas compiler rejects browser globals at the AST boundary", () => {
  test.skip(!typescriptPath, "installed Cursor TypeScript path is not configured");
  const repository = path.resolve(process.cwd(), "..");
  const compile = spawnSync(
    process.execPath,
    [
      path.join(repository, "collector", "collector", "canvas_compile.cjs"),
      /** @type {string} */ (typescriptPath),
      path.join(
        repository,
        "collector",
        "tests",
        "fixtures",
        "unsafe-global.canvas.tsx",
      ),
    ],
    { encoding: "utf8", timeout: 10_000 },
  );
  expect(compile.status).toBe(3);
  expect(compile.stderr).toBe("forbidden_syntax");
});

test("captured Cursor runtime mounts deterministic compiled Canvas", async ({ page }) => {
  test.skip(
    !runtimePath || !typescriptPath,
    "installed Cursor Canvas runtime paths are not configured",
  );

  const repository = path.resolve(process.cwd(), "..");
  const sourcePath = path.join(
    repository,
    "collector",
    "tests",
    "fixtures",
    "safe.canvas.tsx",
  );
  const compilerPath = path.join(
    repository,
    "collector",
    "collector",
    "canvas_compile.cjs",
  );
  const compile = spawnSync(
    process.execPath,
    [compilerPath, /** @type {string} */ (typescriptPath), sourcePath],
    { encoding: "utf8", timeout: 10_000 },
  );
  expect(compile.status, compile.stderr).toBe(0);
  const runtime = readFileSync(/** @type {string} */ (runtimePath), "utf8");
  const nonce = "runtime-test-nonce";
  const runtimeB64 = Buffer.from(runtime).toString("base64");
  const compiledB64 = Buffer.from(compile.stdout).toString("base64");
  const shell = `<!doctype html><html><head>
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}' blob:">
    </head><body><div id="root"></div><script type="module" nonce="${nonce}">
    const bytes = (value) => Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
    const moduleUrl = (value) => URL.createObjectURL(new Blob([bytes(value)], {type:"text/javascript"}));
    const runtime = await import(moduleUrl("${runtimeB64}"));
    await runtime.mountCanvas(moduleUrl("${compiledB64}"));
    </script></body></html>`;

  await page.setContent('<iframe data-testid="runtime-frame" sandbox="allow-scripts"></iframe>');
  await page.getByTestId("runtime-frame").evaluate((frame, source) => {
    if (frame instanceof HTMLIFrameElement) frame.srcdoc = source;
  }, shell);

  const frame = page.frameLocator('[data-testid="runtime-frame"]');
  await expect(frame.getByText("Safe compiler fixture")).toBeVisible({
    timeout: 15_000,
  });
  const sandbox = await page.getByTestId("runtime-frame").getAttribute("sandbox");
  expect(sandbox).toBe("allow-scripts");
  expect(sandbox).not.toContain("allow-same-origin");
});
