# TTB Label Verification — Prototype

AI-powered tool that batch-checks alcohol beverage label images against
their COLA application data. An agent uploads a CSV of applications and
the matching label images (matched by filename), and gets a per-label
review checklist as each one finishes — including a word-for-word check
of the mandatory government health warning.

**Live demo:** _add your Vercel URL here_

## How it works

```
applications.csv ─┐
                  ├─▶ Browser matches rows to images by filename, then
label images ────┘    for each pair (3 at a time):
                            │
                            ▼  resized image + application fields
                        FastAPI ──▶ Gemini 2.5 Flash
                            │        (extracts label text, structured JSON)
                            ▼
                        rules.py
                            │  (Python applies compliance rules)
                            ▼
                  per-label checklist streams into the results panel
```

1. The CSV has one row per label: `image_file, brand_name, class_type,
   alcohol_content, net_contents`. The UI offers a sample CSV download
   and flags unmatched rows/images before anything runs.
2. The frontend resizes each image client-side (max 1600px, JPEG) and
   sends each (image, application row) pair as its own request, three
   at a time.
3. One Gemini vision call per label extracts the fields from the image.
4. Python applies the compliance rules and returns a per-field result.

### Design principle: LLM for extraction, Python for rules

Gemini's job is to read the label image and extract raw values — it
handles the visual noise (stylised fonts, curved bottle text, glare,
bad angles) that traditional OCR can't. For numeric fields like ABV and
net contents it also normalises both the label value and the application
value into a common form so formatting variants ("45% Alc./Vol. (90 Proof)"
vs "45% ALC/VOL") never cause false failures.

Python's job is to apply the compliance rules deterministically:
- Brand name and class/type: case-insensitive string match
- ABV and net contents: numeric equality on the normalised values
- Government warning body: verbatim match (whitespace-normalised)
- Government warning header: must start with "GOVERNMENT WARNING:"
  in all caps per 27 CFR 16.22

Legally exact requirements are never left to model judgment. The model
surfaces an optional note on anything it observes about each field;
that note is shown to the agent for context but does not affect the
pass/fail decision.

### Government warning check

27 CFR 16.21/16.22 requires the warning verbatim with "GOVERNMENT
WARNING:" in capital letters and bold. The check has two independent
parts:

- **Body** — verbatim match after whitespace normalisation. Wrong
  wording or capitalization in the body → mismatch.
- **Header** — must be present and read "GOVERNMENT WARNING:" in all
  caps. Title case → mismatch. Header not detected in the transcription
  → needs review (flagged for the agent to verify visually, since the
  model may have omitted it from the transcription).

Bold detection is best-effort: if the model can't confirm the header
is bold, the result says "verify visually" rather than silently passing.

### Needs review

A label is flagged **needs review** (rather than pass or fail) when the
tool detects a potential issue but cannot make a definitive call — for
example, the warning body is correct but the header wasn't found in the
transcription, or the image quality is too poor to read reliably. The
agent sees the specific reason and makes the final call. In a production
system these would route to a separate queue; for the prototype the agent
simply opens the row and reads the note.

### Performance

The prior vendor pilot failed because results took 30–40s per label.
This prototype makes exactly one model call per label with thinking
disabled; typical end-to-end time is **2–4 seconds**. The elapsed time
is shown with every result so slowness is never invisible.

The free Gemini tier occasionally returns 503 (high demand) — these are
temporary and the UI surfaces a clear message to retry rather than a
generic error.

## Run locally

```bash
git clone <repo>
cd cola-label-checker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=...        # free key from https://aistudio.google.com
uvicorn api.index:app --reload
# open http://127.0.0.1:8000
```

**WSL users:** create the venv from inside WSL (not from Windows) so
the binaries have the correct executable paths.

## Deploy (Vercel)

1. Push this repo to GitHub and import it at vercel.com
   (framework: Other — `vercel.json` routes everything to the FastAPI app).
2. Add `GEMINI_API_KEY` under Project → Settings → Environment Variables.
3. Deploy. No Docker, no build configuration needed.

## Testing it

The `cola_samples/` directory contains everything you need to run a test
batch immediately:

- `sample_labels.csv` — three applications (bourbon, vodka, wine)
- `bottle_bourbon_01.png`, `bottle_vodka_02.png`, `bottle_wine_03.png` — matching label images

Upload `sample_labels.csv` as the application CSV and all three images as
the label images, then click **Verify batch**.

For adversarial testing, real approved label images are available from the
TTB Public COLA Registry at ttbonline.gov/colasonline. Good test cases:
wrong ABV on a label, title-case "Government Warning", missing warning
entirely, glare/angle photo, an image with no CSV row (skipped with a
warning), a CSV row with no image (flagged before the batch runs).

## File structure

```
api/
├── index.py      # FastAPI routing and request validation
├── verifier.py   # Orchestration — calls Gemini then calls rules
├── rules.py      # All compliance matching logic
└── models.py     # Shared Pydantic models
static/
└── index.html    # Entire frontend (HTML + CSS + JS)
requirements.txt
vercel.json
```

## Assumptions and trade-offs

- **CSV format.** Application data is submitted as a CSV for the
  prototype. In production this would pull directly from COLAs Online
  (TTB Form 5100.31) rather than requiring agents to export a CSV.
  Filename matching would be replaced by COLA application ID matching.
- **Field set.** Brand name, class/type, alcohol content, net contents,
  and the government warning — the core fields agents described. Bottler
  name/address and country of origin would be added the same way.
- **Image size.** Images are resized client-side to 1600px max before
  upload. The 4 MB backend limit is a Vercel serverless constraint; in
  practice post-resize images are 300–800 KB and never approach it.
- **Free-tier Gemini.** Google's free AI Studio tier may use submitted
  data for training — fine for synthetic test labels, not for production.
  The free tier occasionally returns 503 errors under high demand — the UI
  surfaces a clear message so agents know to retry rather than assume a bug.
  Concurrency is capped at 3 (`CONCURRENCY` in `static/index.html`) to stay
  under the 15 requests/minute free-tier limit; a paid key supports 10+
  concurrent requests and still costs pennies per label. A production
  deployment inside TTB's network would need a FedRAMP-authorized endpoint
  (e.g. Gemini on Google Cloud Assured Workloads or an Azure-hosted model,
  given the agency's Azure footprint). The model call is isolated in
  `verifier.py` so the provider is swappable.
- **No auth or rate limiting.** Out of scope for a prototype that stores
  nothing and handles no PII.
- **Vercel serverless.** Chosen for zero-cost hosting with ~1s cold
  starts. Trade-off: no persistent process, so no server-side caching.
- **Bold detection is best-effort.** Whether text is bold is a visual
  property a transcription can't prove; the tool flags uncertainty
  instead of guessing.

## Stack

FastAPI · Gemini 2.5 Flash · vanilla HTML/JS · Vercel serverless.
No frontend framework — the audience is compliance agents, not
developers, and the UI is a single page with two panels and one button
by design.