// Renders the .mmd sources in screenshots/ to PNG, and captures the live
// dashboard. Uses the Chrome already installed on this machine rather than
// downloading a second one.
import { readFile, writeFile, readdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import puppeteer from "puppeteer-core";

const here = path.dirname(fileURLToPath(import.meta.url));
const shots = here;
const mermaidJs = pathToFileURL(
  path.join(here, "node_modules", "mermaid", "dist", "mermaid.min.js")
).href;

const CHROME = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => existsSync(p));

const FONTS =
  "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@500&display=swap";

// Mermaid draws its own title with a different CSS class per diagram type, so
// styling it consistently means chasing several selectors. Pulling the title
// out of the frontmatter and drawing it as HTML gives every diagram the same
// heading, and leaves the .mmd portable for any other renderer.
function splitTitle(src) {
  const m = src.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n/);
  if (!m) return { title: "", defn: src };
  const t = m[1].match(/^title:\s*(.+)$/m);
  return { title: t ? t[1].trim() : "", defn: src.slice(m[0].length) };
}

function page(src) {
  const { title, defn } = splitTitle(src);
  return `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="${FONTS}">
<script src="${mermaidJs}"></script>
<style>
  html,body{margin:0;background:#FBF7EF;}
  #frame{display:inline-block;padding:30px 38px 34px;background:#FFFDF8;
    border:4px solid #14131A;box-shadow:12px 12px 0 #14131A;margin:30px;}
  h1{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:27px;
    color:#14131A;margin:0 0 22px;letter-spacing:-0.01em;}
  h1 span{display:block;width:64px;height:6px;background:#B8F14A;
    border:2px solid #14131A;margin-top:10px;}
  #out svg{display:block;height:auto;max-width:none;}
  .nodeLabel,.edgeLabel,.messageText,.loopText,.noteText,.actor,
  .labelText,.sectionTitle{font-family:'Inter',system-ui,sans-serif !important;}
  .edgeLabel{background:#FFFDF8 !important;}
</style></head><body>
<div id="frame">${title ? `<h1>${title}<span></span></h1>` : ""}<div id="out"></div></div>
<script type="module">
  window.__done = (async () => {
    // document.fonts.ready resolves instantly while nothing on the page uses
    // the webfont, so mermaid would measure with the fallback and then get
    // wider glyphs once Inter arrives - which is what clips node labels.
    try {
      await Promise.all([
        document.fonts.load('400 15px Inter'),
        document.fonts.load('600 15px Inter'),
        document.fonts.load('800 15px Inter'),
        document.fonts.load('500 15px "JetBrains Mono"'),
        document.fonts.load('700 24px "Space Grotesk"'),
      ]);
      await document.fonts.ready;
    } catch {}
    mermaid.initialize({
      startOnLoad:false, theme:'base', securityLevel:'loose',
      fontFamily:"Inter, system-ui, sans-serif",
      themeVariables:{
        background:'#FFFDF8', primaryColor:'#FFFFFF', primaryTextColor:'#14131A',
        primaryBorderColor:'#14131A', lineColor:'#14131A', textColor:'#14131A',
        fontSize:'15px',
        clusterBkg:'#F3EFE6', clusterBorder:'#14131A',
        actorBkg:'#FFD23F', actorBorder:'#14131A', actorTextColor:'#14131A',
        actorLineColor:'#14131A', signalColor:'#14131A', signalTextColor:'#14131A',
        labelBoxBkgColor:'#B8F14A', labelBoxBorderColor:'#14131A',
        labelTextColor:'#14131A', loopTextColor:'#14131A',
        noteBkgColor:'#FFF3C4', noteBorderColor:'#14131A', noteTextColor:'#14131A',
        sequenceNumberColor:'#14131A'
      },
      flowchart:{ htmlLabels:true, curve:'basis', nodeSpacing:52, rankSpacing:70,
                  padding:16, useMaxWidth:false, wrappingWidth:340 },
      sequence:{ useMaxWidth:false, wrap:false, width:190, boxMargin:12,
                 noteMargin:12, messageMargin:44, mirrorActors:false,
                 actorFontWeight:700 }
    });
    const { svg } = await mermaid.render('g', ${JSON.stringify(defn)});
    document.getElementById('out').innerHTML = svg;
    try { await document.fonts.ready; } catch {}
    return true;
  })();
</script></body></html>`;
}

async function shoot(browser, html, out, scale = 2) {
  // Navigated as a real file:// document: a script tag pointing at file:// is
  // refused when the page origin is about:blank, which setContent gives you.
  const tmp = path.join(here, "_render.html");
  await writeFile(tmp, html, "utf8");
  const p = await browser.newPage();
  p.on("pageerror", (e) => console.log("  ! page error:", e.message));
  await p.setViewport({ width: 1600, height: 1000, deviceScaleFactor: scale });
  await p.goto(pathToFileURL(tmp).href, { waitUntil: "networkidle0", timeout: 60000 });
  await p.evaluate(() => window.__done);
  const el = await p.$("#frame");
  await el.screenshot({ path: out });
  const { w, h } = await p.evaluate(() => {
    const r = document.getElementById("frame").getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height) };
  });
  console.log(`  ${path.basename(out)}  ${w}x${h} css @${scale}x`);
  await p.close();
}

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: "new",
  args: ["--allow-file-access-from-files", "--font-render-hinting=none"],
});

console.log("diagrams:");
for (const f of (await readdir(shots)).filter((f) => f.endsWith(".mmd"))) {
  const defn = await readFile(path.join(shots, f), "utf8");
  await shoot(browser, page(defn), path.join(shots, f.replace(/\.mmd$/, ".png")));
}

const url = process.argv[2];
if (url) {
  console.log("dashboard:");
  for (const [name, w, full] of [
    ["dashboard-full.png", 1440, true],
    ["dashboard-top.png", 1440, false],
  ]) {
    const p = await browser.newPage();
    await p.setViewport({ width: w, height: 1050, deviceScaleFactor: 2 });
    await p.goto(url, { waitUntil: "networkidle0", timeout: 90000 });
    await new Promise((r) => setTimeout(r, 3500));
    await p.screenshot({ path: path.join(shots, name), fullPage: full });
    console.log(`  ${name}`);
    await p.close();
  }

  // The PII contract is the most interesting scorecard in the set, so open it
  // deliberately rather than shipping whatever happened to be selected first.
  const p = await browser.newPage();
  await p.setViewport({ width: 1500, height: 1200, deviceScaleFactor: 2 });
  await p.goto(url, { waitUntil: "networkidle0", timeout: 90000 });
  await new Promise((r) => setTimeout(r, 3500));
  const picked = await p.evaluate(() => {
    const b = [...document.querySelectorAll(".run")].find((x) =>
      /PII detection/i.test(x.textContent)
    );
    if (!b) return null;
    b.click();
    return b.textContent.replace(/\s+/g, " ").trim();
  });
  await new Promise((r) => setTimeout(r, 3000));
  const sec = await p.evaluateHandle(() =>
    [...document.querySelectorAll("section")].find((s) =>
      /Behaviour contracts/i.test(s.querySelector("h2")?.textContent || "")
    )
  );
  const el = sec.asElement();
  if (el) {
    await el.screenshot({ path: path.join(shots, "contract-scorecard.png") });
    console.log(`  contract-scorecard.png  (selected: ${picked ?? "default"})`);
  }
  await p.close();
}

await browser.close();
