// A minimal MCP Apps host, for tests/test_apps_view.py.
//
// Loads a view's HTML in a sandboxed iframe (allow-scripts only, so its
// origin is opaque) under the restrictive default CSP the Apps spec asks hosts
// to apply when a resource declares none, speaks the host side of the
// postMessage JSON-RPC, and forwards the view's tools/call to a real nuvel-mcp
// over Streamable HTTP (2026-07-28). Prints one JSON report on stdout.
//
//   node tests/apps_host.js <mcp-url> <view-html-file> <screenshot-dir>

"use strict";
const fs = require("fs");
const http = require("http");
const path = require("path");
const { chromium } = require("playwright");

const [mcpUrl, viewFile, shotDir] = process.argv.slice(2);
const DEFAULT_CSP = "default-src 'none'; script-src 'self' 'unsafe-inline'; " +
  "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' data:; " +
  "connect-src 'none'";

let nextId = 1;
function callServer(method, params) {
  const body = JSON.stringify({ jsonrpc: "2.0", id: nextId++, method, params: {
    ...params,
    _meta: { "io.modelcontextprotocol/protocolVersion": "2026-07-28",
             "io.modelcontextprotocol/clientCapabilities": {
               extensions: { "io.modelcontextprotocol/ui": { mimeTypes: ["text/html;profile=mcp-app"] } } } },
  } });
  const headers = { "Content-Type": "application/json", Accept: "application/json",
                    "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method };
  if (method === "tools/call") headers["Mcp-Name"] = params.name;
  if (method === "resources/read") headers["Mcp-Name"] = params.uri;
  return new Promise((resolve, reject) => {
    const req = http.request(mcpUrl, { method: "POST", headers }, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => resolve(JSON.parse(data)));
    });
    req.on("error", reject);
    req.end(body);
  });
}

function hostPage(viewHtml) {
  // The CSP and a violation reporter go first in the view's <head>.
  const guard = `<meta http-equiv="Content-Security-Policy" content="${DEFAULT_CSP}">` +
    `<script>document.addEventListener("securitypolicyviolation", (e) =>` +
    ` parent.postMessage({ cspViolation: e.violatedDirective }, "*"));</script>`;
  const doc = viewHtml.replace("<head>", "<head>" + guard);
  const srcdoc = doc.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  return `<!DOCTYPE html><html><body style="margin:0;background:#000">
<iframe id="view" sandbox="allow-scripts" style="border:0;width:100%;height:100vh"
  srcdoc="${srcdoc}"></iframe>
<script>
  window.log = { methods: [], violations: [], messages: [], sizes: [] };
  const frame = document.getElementById("view");
  const toView = (m) => frame.contentWindow.postMessage(m, "*");
  window.addEventListener("message", async (event) => {
    if (event.source !== frame.contentWindow) return;
    const m = event.data;
    if (m && m.cspViolation) { log.violations.push(m.cspViolation); return; }
    if (!m || m.jsonrpc !== "2.0") return;
    log.methods.push(m.method || "response");
    if (m.method === "ui/initialize") {
      toView({ jsonrpc: "2.0", id: m.id, result: {
        protocolVersion: "2026-01-26",
        hostInfo: { name: "test-host", version: "0" },
        hostCapabilities: { serverTools: {} },
        hostContext: { theme: "dark", displayMode: "inline",
                       styles: { variables: { "--color-background-primary": "#101014" } } } } });
    } else if (m.method === "ui/notifications/initialized") {
      // The model called browse_skills: send its input, run it, send the result.
      const args = { query: "roll" };
      toView({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: args } });
      const reply = await callServer("tools/call", { name: "browse_skills", arguments: args });
      toView({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: reply.result });
    } else if (m.method === "tools/call") {
      const reply = await callServer("tools/call", m.params);
      toView({ jsonrpc: "2.0", id: m.id, ...(reply.error ? { error: reply.error } : { result: reply.result }) });
    } else if (m.method === "ui/message") {
      log.messages.push(m.params);
      toView({ jsonrpc: "2.0", id: m.id, result: {} });
    } else if (m.method === "ui/notifications/size-changed") {
      log.sizes.push(m.params);
    }
  });
</script></body></html>`;
}

(async () => {
  const browser = await chromium.launch();
  const report = {};
  try {
    for (const [label, viewport] of [["desktop", { width: 720, height: 560 }],
                                     ["phone", { width: 390, height: 700 }]]) {
      const page = await browser.newPage({ viewport, deviceScaleFactor: 2 });
      await page.exposeFunction("callServer", callServer);
      await page.setContent(hostPage(fs.readFileSync(viewFile, "utf-8")));
      const view = page.frameLocator("#view");
      await view.locator(".item").first().waitFor();
      const r = (report[label] = {});
      r.firstResults = await view.locator(".item .name").allTextContents();
      r.background = await view.locator("body").evaluate((b) => getComputedStyle(b).backgroundColor);
      r.overflowX = await view.locator("html").evaluate((h) => h.scrollWidth > h.clientWidth);
      await page.screenshot({ path: path.join(shotDir, `${label}-results.png`) });

      await view.locator("#query").fill("onboarding");
      await view.locator("#query").press("Enter");
      await view.locator(".item .name", { hasText: "onboarding" }).waitFor();
      r.searchResults = await view.locator(".item .name").allTextContents();
      await view.locator(".item", { hasText: "onboarding" }).click();
      await view.locator("#skill-body", { hasText: "Employee onboarding" }).waitFor();
      r.skillBody = await view.locator("#skill-body").textContent();
      await page.screenshot({ path: path.join(shotDir, `${label}-skill.png`) });

      await view.locator("#use").click();
      await page.waitForFunction(() => window.log.messages.length > 0);
      r.log = await page.evaluate(() => window.log);
      await page.close();
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(report));
})().catch((err) => { console.error(err); process.exit(1); });
