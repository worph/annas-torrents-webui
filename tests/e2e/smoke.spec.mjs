import { test, expect } from "@playwright/test";

const TOKEN = process.env.E2E_API_TOKEN || "e2e-playwright-token";

async function withToken(page) {
  await page.addInitScript((t) => {
    localStorage.setItem("annas_api_token", t);
  }, TOKEN);
}

test("home shows connecting pill with API token", async ({ page }) => {
  await withToken(page);
  await page.goto("/");
  await expect(page.locator("#conn-pill")).toBeVisible();
  await expect(page.locator("h1")).toContainText("Anna's Torrents");
});

test("view mode hides controls without relying on JS", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto("/view");
  await expect(page.locator("body.view-mode")).toHaveCount(1);
  await expect(page.locator("#global-controls")).toBeHidden();
  await expect(page.locator("#settings-btn")).toBeHidden();
  await context.close();
});

test("private status requires token (auth gate)", async ({ page, request }) => {
  const bare = await request.get("/api/status");
  expect(bare.status()).toBe(401);

  const ok = await request.get("/api/status", {
    headers: { "X-API-Token": TOKEN },
  });
  expect(ok.ok()).toBeTruthy();

  await withToken(page);
  await page.goto("/");
  await expect(page.locator("#conn-pill")).toBeVisible();
  await expect
    .poll(async () => (await page.locator("#conn-pill").textContent()) || "", { timeout: 20_000 })
    .not.toMatch(/Connecting/i);
});

test("security headers present on HTML", async ({ request }) => {
  const r = await request.get("/");
  expect(r.headers()["content-security-policy"] || "").toMatch(/default-src 'self'/);
  expect(r.headers()["x-frame-options"] || "").toMatch(/DENY/i);
});

test("SSE ticket is one-shot then reconnect path works", async ({ request }) => {
  const issued = await request.get("/api/events/ticket", {
    headers: { "X-API-Token": TOKEN },
  });
  expect(issued.ok()).toBeTruthy();
  const { ticket } = await issued.json();
  expect(ticket).toBeTruthy();

  const first = await request.get(`/api/events?ticket=${encodeURIComponent(ticket)}`);
  // StreamingResponse may stay open — cancel after headers prove auth accepted.
  expect(first.status()).toBe(200);
  await first.body().cancel().catch(() => {});

  const reuse = await request.get(`/api/events?ticket=${encodeURIComponent(ticket)}`);
  expect(reuse.status()).toBe(401);

  const again = await request.get("/api/events/ticket", {
    headers: { "X-API-Token": TOKEN },
  });
  expect(again.ok()).toBeTruthy();
  const { ticket: ticket2 } = await again.json();
  expect(ticket2).toBeTruthy();
  expect(ticket2).not.toEqual(ticket);
});
