# Screenshots and diagrams

Every `.png` here is generated, not hand-drawn. The `.mmd` files are the
Mermaid sources; the dashboard shots are real captures of the deployed Function
URL, not mockups.

## Captions

Ready to paste under each image in the Builder Center article.

### `dashboard-top.png`
> The live dashboard. `NO INFERENCE` is not a rendering bug — that is the radar
> correctly reporting that this AWS account cannot call Bedrock at all, while
> Comprehend and Translate answer fine. This is exactly the state that was
> silently breaking my other agent.

### `architecture.png` — source: `architecture.mmd`
> Two Lambdas and no build system. `providers.py` is the seam that let the whole
> thing survive losing Bedrock.

### `daily-sweep.png` — source: `daily-sweep.mmd`
> One unattended sweep, end to end: scan, diff against yesterday, then replay
> every behaviour contract. Note the `alt` block — silence is a deliberate
> output, not a missing feature.

### `verdict-rules.png` — source: `verdict-rules.mmd`
> The whole grader. It is a decision tree in plain Python — no model sits
> anywhere on this path.

### `contract-scorecard.png`
> The stricter-threshold result, measured rather than assumed. Same service,
> same inputs, same price — `min=0.999` drops to 18/27 and is the only row that
> fails outright. The amber banner is the tool admitting its own baseline is
> imperfect, which felt more useful than hiding it.

### `dashboard-full.png`
> The full page: reachability, the change timeline, and every behaviour contract
> the radar replayed on its last scheduled sweep.

## Regenerating

Needs Node and a Chrome or Edge already installed — `puppeteer-core` drives the
local browser instead of downloading a second one.

```bash
cd screenshots
npm install
node render.mjs                                  # diagrams only
node render.mjs "https://<your-function-url>/"   # diagrams + live dashboard
node validate.mjs ../docs/ARTICLE.md             # parse the article's mermaid
```

`render.mjs` renders every `.mmd` in this folder, so adding a diagram means
adding one file. The Mermaid sources are the artifact that matters — any
Mermaid renderer will produce the same diagrams from them.
