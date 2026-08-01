// @ts-check
import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { FIXTURE_TOKEN, FIXTURE_USER } from "./fixtures/conversation-scenarios.mjs";
import {
  butterbridgeIdentities,
  deviceGroups,
  groupedCursorFiles,
  rawDevices,
} from "./fixtures/device-groups.mjs";

const artifactDir = path.resolve(
  process.cwd(),
  "..",
  "artifacts",
  "sidebar-device-grouping",
);
fs.mkdirSync(artifactDir, { recursive: true });

function collectPageErrors(page) {
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  return { consoleErrors, pageErrors };
}

function expectNoPageErrors(errors) {
  expect(errors.consoleErrors).toEqual([]);
  expect(errors.pageErrors).toEqual([]);
}

async function installDeviceMocks(page) {
  await page.addInitScript((token) => {
    localStorage.setItem("dr_token", token);
    localStorage.setItem("dr_remember_me", "1");
    localStorage.setItem("dr_locale", "en-US");
  }, FIXTURE_TOKEN);

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    const fulfill = (json, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(json),
      });

    if (pathname === "/api/events/stream") {
      await route.fulfill({ status: 204, body: "" });
    } else if (pathname === "/api/events/session") {
      await fulfill({ ok: true });
    } else if (pathname === "/api/auth/me") {
      await fulfill(FIXTURE_USER);
    } else if (pathname === "/api/auth/refresh") {
      await fulfill({
        access_token: FIXTURE_TOKEN,
        token_type: "bearer",
        user_id: FIXTURE_USER.id,
        role: FIXTURE_USER.role,
      });
    } else if (pathname === "/api/auth/registration-mode") {
      await fulfill({ mode: "closed", has_any_user: true, github_enabled: false });
    } else if (pathname === "/api/hierarchy/devices") {
      await fulfill(deviceGroups);
    } else if (pathname === "/api/devices") {
      await fulfill(rawDevices);
    } else if (/^\/api\/devices\/[^/]+\/discovery$/.test(pathname)) {
      await fulfill({ tools: {} });
    } else if (/^\/api\/tools\/cursor$/.test(pathname)) {
      await fulfill({
        id: "cursor",
        display_name: "Cursor",
        total_files: 500,
        total_size_bytes: 0,
        last_sync_at: null,
        categories: { conversation: 500 },
      });
    } else if (/\/api\/hierarchy\/devices\/[^/]+\/tools\/cursor\/projects$/.test(pathname)) {
      await fulfill([]);
    } else if (/\/api\/hierarchy\/devices\/[^/]+\/tools\/cursor\/files$/.test(pathname)) {
      await fulfill({
        total: groupedCursorFiles.length,
        files: groupedCursorFiles,
        project: null,
      });
    } else if (request.method() === "GET") {
      await fulfill([]);
    } else {
      await fulfill({});
    }
  });
}

async function openShell(page, viewport) {
  await page.setViewportSize(viewport);
  await installDeviceMocks(page);
  await page.goto("/search");
  await expect(page.locator('[data-testid="sidebar-host"]')).toHaveCount(2);
}

function host(page, name) {
  return page.locator(`[data-testid="sidebar-host"][data-host-name="${name}"]`);
}

async function sidebarX(page) {
  return page.locator('[data-testid="app-sidebar"]').evaluate(
    (element) => element.getBoundingClientRect().x,
  );
}

async function expectSidebarOpen(page) {
  await expect.poll(() => sidebarX(page)).toBeGreaterThanOrEqual(0);
}

async function expectSidebarClosed(page) {
  await expect.poll(() => sidebarX(page)).toBeLessThan(0);
}

test("desktop groups hosts, aggregates filters, and preserves child access", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openShell(page, { width: 1440, height: 900 });

  const butterbridge = host(page, "butterbridge");
  const dreamland = host(page, "dreamland-yoga");
  await expect(butterbridge).toHaveCount(1);
  await expect(dreamland).toHaveCount(1);
  await expect(butterbridge.locator("button").first()).toContainText("649");
  await expect(dreamland.locator("button").first()).toContainText("4084");
  await expect(page.locator("header select option")).toHaveCount(3);
  await expect(page.locator('[data-testid="sidebar-version"]')).toHaveText("v0.2.0");

  await butterbridge.locator("button").first().click();
  await expect(butterbridge.locator('[data-testid="sidebar-identity"]')).toHaveCount(6);
  const identityLabels = await butterbridge
    .locator('[data-testid="sidebar-identity"] > button')
    .allTextContents();
  expect(new Set(identityLabels).size).toBe(identityLabels.length);

  const aggregateCursor = butterbridge.locator(
    '[data-testid="sidebar-tool-link"][data-device-scope="host_butterbridge"][data-tool-id="cursor"]',
  );
  await aggregateCursor.click();
  await expect(page).toHaveURL(/\/devices\/host_butterbridge\/tools\/cursor$/);
  await expect(page.getByText("Windows conversation included")).toBeVisible();
  await expect(page.getByText("WSL conversation included")).toBeVisible();
  await expect(aggregateCursor).toHaveAttribute("aria-current", "page");

  const activeHost = host(page, "butterbridge");
  const windowsIdentity = activeHost.locator(
    `[data-testid="sidebar-identity"][data-device-id="${butterbridgeIdentities[4].device_id}"]`,
  );
  await windowsIdentity.locator("button").first().click();
  const childCursor = windowsIdentity.locator(
    `[data-testid="sidebar-tool-link"][data-device-scope="${butterbridgeIdentities[4].device_id}"][data-tool-id="cursor"]`,
  );
  await expect(childCursor).toBeVisible();
  await childCursor.click();
  await expect(page).toHaveURL(
    new RegExp(`/devices/${butterbridgeIdentities[4].device_id}/tools/cursor$`),
  );
  await expect(childCursor).toHaveAttribute("aria-current", "page");
  await expect(aggregateCursor).not.toHaveAttribute("aria-current", "page");

  const overflow = await page.locator('[data-testid="app-sidebar"]').evaluate(
    (element) => element.scrollWidth - element.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({
    path: path.join(artifactDir, "desktop-after.png"),
    fullPage: true,
  });
  expectNoPageErrors(errors);
});

test("mobile defaults compact and keeps drawer behavior while filtering a group", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openShell(page, { width: 390, height: 844 });
  await expectSidebarClosed(page);

  await page.getByRole("button", { name: "Menu" }).click();
  await expectSidebarOpen(page);
  const butterbridge = host(page, "butterbridge");
  const dreamland = host(page, "dreamland-yoga");
  await expect(butterbridge).toHaveCount(1);
  await expect(dreamland).toHaveCount(1);
  await expect(butterbridge.locator("button").first()).toHaveAttribute("aria-expanded", "false");
  await expect(dreamland.locator("button").first()).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator('[data-testid="sidebar-version"]')).toHaveText("v0.2.0");
  await page.screenshot({
    path: path.join(artifactDir, "mobile-after.png"),
    fullPage: true,
  });

  await butterbridge.locator("button").first().click();
  await expect(butterbridge.locator('[data-testid="sidebar-identity"]')).toHaveCount(6);
  const overflow = await page.locator('[data-testid="app-sidebar"]').evaluate(
    (element) => element.scrollWidth - element.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);

  await butterbridge.locator(
    '[data-testid="sidebar-tool-link"][data-device-scope="host_butterbridge"][data-tool-id="cursor"]',
  ).click();
  await expect(page).toHaveURL(/\/devices\/host_butterbridge\/tools\/cursor$/);
  await expect(page.getByText("Windows conversation included")).toBeVisible();
  await expect(page.getByText("WSL conversation included")).toBeVisible();
  await expectSidebarClosed(page);

  await page.getByRole("button", { name: "Menu" }).click();
  await expectSidebarOpen(page);
  await expect(host(page, "butterbridge").locator("button").first()).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await page.locator('[data-testid="sidebar-overlay"]').click({ position: { x: 300, y: 20 } });
  await expectSidebarClosed(page);

  await page.getByRole("button", { name: "Menu" }).click();
  await page.getByRole("button", { name: "Close" }).click();
  await expectSidebarClosed(page);
  expectNoPageErrors(errors);
});
