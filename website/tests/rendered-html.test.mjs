import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render(pathname) {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${pathname}`, {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the project website", async () => {
  const response = await render("/");
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>Personal Trading Decision System \(IBKR\)<\/title>/i);
  assert.match(html, /Decisions[\s\S]*before[\s\S]*orders/);
  assert.match(html, /Open synthetic demo/);
  assert.match(html, /No broker connection\. No real account data\. No persistence\./);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton|Your site is taking shape/i);
});

test("server-renders a privacy-safe synthetic demo", async () => {
  const response = await render("/demo");
  assert.equal(response.status, 200);

  const html = await response.text();
  assert.match(html, /Synthetic data only/);
  assert.match(html, /Every account, symbol, price and event below is fabricated/);
  assert.match(html, /No network data/);
  assert.match(html, /SIM-A/);
  assert.match(html, /Manual review required before any broker action/);
  assert.doesNotMatch(html, /placeOrder|reqAccountUpdates|accountId|TOTP|token/i);
});

test("keeps the public site free of template and persistence features", async () => {
  const [page, demo, packageJson, hosting] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/demo/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(new URL("../.openai/hosting.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /Open synthetic demo/);
  assert.match(demo, /SIM-A/);
  assert.doesNotMatch(`${page}\n${demo}\n${packageJson}`, /_sites-preview|react-loading-skeleton|drizzle/i);
  assert.doesNotMatch(demo, /fetch\(|localStorage|sessionStorage|document\.cookie/i);
  const hostingConfig = JSON.parse(hosting);
  assert.match(hostingConfig.project_id, /^appgprj_[a-z0-9]+$/);
  assert.equal(hostingConfig.d1, null);
  assert.equal(hostingConfig.r2, null);
});
