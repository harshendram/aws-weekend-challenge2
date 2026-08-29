// Parses every ```mermaid block in a markdown file. Shipping an article with a
// diagram that silently fails to render is a bad way to find out.
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { existsSync } from "node:fs";
import puppeteer from "puppeteer-core";

const here = path.dirname(fileURLToPath(import.meta.url));
const mermaidJs = pathToFileURL(
  path.join(here, "node_modules", "mermaid", "dist", "mermaid.min.js")
).href;
const CHROME = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => existsSync(p));

const md = await readFile(process.argv[2], "utf8");
const blocks = [...md.matchAll(/```mermaid\r?\n([\s\S]*?)```/g)].map((m) => m[1]);
console.log(`found ${blocks.length} mermaid block(s)`);

const html = `<!doctype html><meta charset="utf-8"><script src="${mermaidJs}"></script>
<script>window.__ready = new Promise(r=>{const t=setInterval(()=>{
  if(window.mermaid){clearInterval(t);mermaid.initialize({startOnLoad:false});r(1);}},20);});</script>`;
const tmp = path.join(here, "_validate.html");
await writeFile(tmp, html, "utf8");

const browser = await puppeteer.launch({
  executablePath: CHROME, headless: "new",
  args: ["--allow-file-access-from-files"],
});
const p = await browser.newPage();
await p.goto(pathToFileURL(tmp).href, { waitUntil: "networkidle0" });
await p.evaluate(() => window.__ready);

let bad = 0;
for (const [i, defn] of blocks.entries()) {
  const err = await p.evaluate(async (d) => {
    try { await mermaid.parse(d); return null; } catch (e) { return String(e.message || e); }
  }, defn);
  if (err) { bad++; console.log(`block ${i + 1}: FAILED\n${err}`); }
  else console.log(`block ${i + 1}: ok (${defn.trim().split("\n")[0]})`);
}
await browser.close();
process.exit(bad ? 1 : 0);
