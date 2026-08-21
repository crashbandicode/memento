// @ts-check
import { expect, test } from "@playwright/test";

import { FIXTURE_TOKEN, FIXTURE_USER } from "./fixtures/conversation-scenarios.mjs";
import { seedAuth } from "./support/conversation-page.mjs";

const JSON_HEADERS = { "content-type": "application/json" };

test("rejected TOTP re-authentication keeps the valid session and shows an inline error", async ({ page }) => {
  await seedAuth(page);
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;

    if (request.method() === "GET" && pathname === "/api/auth/me") {
      await route.fulfill({ status: 200, headers: JSON_HEADERS, body: JSON.stringify(FIXTURE_USER) });
      return;
    }
    if (request.method() === "POST" && pathname === "/api/auth/refresh") {
      await route.fulfill({
        status: 200,
        headers: JSON_HEADERS,
        body: JSON.stringify({
          access_token: FIXTURE_TOKEN,
          token_type: "bearer",
          user_id: FIXTURE_USER.id,
          role: FIXTURE_USER.role,
        }),
      });
      return;
    }
    if (request.method() === "POST" && pathname === "/api/auth/me/totp/setup") {
      await route.fulfill({
        status: 403,
        headers: JSON_HEADERS,
        body: JSON.stringify({ detail: "Current password is incorrect" }),
      });
      return;
    }
    if (pathname === "/api/events/stream") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (request.method() === "GET" && pathname === "/api/hierarchy/devices") {
      await route.fulfill({ status: 200, headers: JSON_HEADERS, body: "[]" });
      return;
    }
    await route.fulfill({ status: 200, headers: JSON_HEADERS, body: "{}" });
  });

  await page.goto("/profile");
  const security = page.getByRole("region", { name: "Security" });
  await security.getByLabel("Current password").fill("wrong password");
  await security.getByRole("button", { name: "Set up TOTP" }).click();

  await expect(page).toHaveURL(/\/profile$/);
  await expect(security.getByRole("alert")).toContainText("Current password is incorrect");
  await expect(page.evaluate(() => localStorage.getItem("dr_token"))).resolves.toBe(FIXTURE_TOKEN);
});

test("TOTP setup can be confirmed and updates the profile state", async ({ page }) => {
  await seedAuth(page);
  let enabled = false;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;

    if (request.method() === "GET" && pathname === "/api/auth/me") {
      await route.fulfill({
        status: 200,
        headers: JSON_HEADERS,
        body: JSON.stringify({ ...FIXTURE_USER, totp_enabled: enabled }),
      });
      return;
    }
    if (request.method() === "POST" && pathname === "/api/auth/refresh") {
      await route.fulfill({
        status: 200,
        headers: JSON_HEADERS,
        body: JSON.stringify({ access_token: FIXTURE_TOKEN, user_id: FIXTURE_USER.id, role: FIXTURE_USER.role }),
      });
      return;
    }
    if (request.method() === "POST" && pathname === "/api/auth/me/totp/setup") {
      await route.fulfill({
        status: 200,
        headers: JSON_HEADERS,
        body: JSON.stringify({
          secret: "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
          provisioning_uri: "otpauth://totp/Memento%3Atotp-regression%40memento.test?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&issuer=Memento",
        }),
      });
      return;
    }
    if (request.method() === "POST" && pathname === "/api/auth/me/totp/confirm") {
      enabled = true;
      await route.fulfill({
        status: 200,
        headers: JSON_HEADERS,
        body: JSON.stringify({ ...FIXTURE_USER, totp_enabled: true }),
      });
      return;
    }
    if (pathname === "/api/events/stream") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (request.method() === "GET" && pathname === "/api/hierarchy/devices") {
      await route.fulfill({ status: 200, headers: JSON_HEADERS, body: "[]" });
      return;
    }
    await route.fulfill({ status: 200, headers: JSON_HEADERS, body: "{}" });
  });

  await page.goto("/profile");
  const security = page.getByRole("region", { name: "Security" });
  await security.getByLabel("Current password").fill("correct horse battery staple");
  await security.getByRole("button", { name: "Set up TOTP" }).click();
  await expect(security.getByText("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")).toBeVisible();

  await security.getByLabel("6-digit authenticator code").fill("287082");
  await security.getByRole("button", { name: "Confirm TOTP" }).click();

  await expect(security.getByText("Enabled", { exact: true })).toBeVisible();
  await expect(security.getByLabel("Current password")).toHaveValue("");
});
