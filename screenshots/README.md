# Screenshots and diagrams

Every `.png` here is generated, not hand-drawn. The `.mmd` files are the Mermaid
sources; the dashboard shots are real captures of the deployed Function URL, not
mockups.

## Used in the article

Four images, in the order they appear. Captions are ready to paste under each
one in the Builder Center editor.

### 1. `dashboard-top.png`
> Screenshot 1 — the live dashboard. `NO INFERENCE` is not a rendering bug. That
> is the radar correctly reporting that this AWS account cannot call Bedrock at
> all, while Comprehend and Translate answer fine. This is exactly the state
> that was silently breaking my other agent, and nothing else I own would have
> told me.

### 2. `reachability.png`
> Screenshot 2 — the reachability panel, which is the whole thesis in one view.
> Three Bedrock targets refuse the call, four Comprehend and Translate targets
> answer in 235–833 ms, and the account is short 151 out of 151 inference
> quotas. The row of region counts underneath is the catalog: 329 models AWS
> will happily list for an account that cannot invoke a single one of them.

### 3. `contract-scorecard.png`
> Screenshot 3 — the stricter-threshold result, measured rather than assumed.
> Same service, same inputs, same published price: `min=0.999` falls to 18/27
> and is the only row that fails outright, while the four regional variants sit
> at 24/27. The amber banner is the tool admitting that its own baseline does
> not satisfy the contract, which felt more honest than hiding it.

### 4. `architecture.png` — source: `architecture.mmd`
> Architecture — two Lambdas and no build system. `providers.py` is the seam
> that let the whole thing survive losing Bedrock: everything above it speaks in
> targets and assertions, and nothing above it knows which AWS service answered.

## Spares

Not in the article, but ready if you want to lengthen it.

### `dashboard-full.png`
> The full page: reachability, the change timeline, and every behaviour contract
> the radar replayed on its last scheduled sweep.

### `daily-sweep.png` — source: `daily-sweep.mmd`
> One unattended sweep, end to end: scan, diff against yesterday, then replay
> every behaviour contract. Note the `alt` block — staying silent is a
> deliberate output, not a missing feature.

### `verdict-rules.png` — source: `verdict-rules.mmd`
> The whole grader. It is a decision tree in plain Python — no model sits
> anywhere on this path.

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
adding one file. Dashboard sections are matched on their visible heading, so a
reordered dashboard fails loudly instead of quietly saving the wrong crop. The
Mermaid sources are the artifact that matters — any Mermaid renderer will
produce the same diagrams from them.
