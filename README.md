# TTB Label Verification — Prototype

AI-powered tool that batch-checks alcohol beverage label images against their
COLA application data. An agent uploads a CSV of applications and the matching
label images (matched by filename), and gets a per-label review checklist as
each one finishes — including a word-for-word check of the mandatory
government health warning. Batches were the headline request from the
compliance team: big importers submit 200–300 applications at once.

**Live demo:** _add your Vercel URL here_

## How it works

```
applications.csv ─┐
                  ├─▶ Browser matches rows to images by filename, then for
label images ────┘    each pair (3 at a time):
                            │
                            ▼  resized image + application fields
                        FastAPI ──▶ Gemini 2.5 Flash (extracts label text
                            │        + fuzzy comparisons, structured JSON)
                            ▼
                  per-label checklist streams into the results panel
```

1. The CSV has one row per label: `image_file, brand_name, class_type,
   alcohol_content, net_contents`. The UI offers a sample CSV download and
   flags unmatched rows/images before anything runs.
2. The frontend resizes each image client-side (max 1600 px, JPEG) and sends
   each (image, application row) pair as its own request, three at a time.
3. One Gemini vision call per label extracts the fields verbatim and makes
   fuzzy equivalence judgments (structured output, temperature 0, thinking
   disabled for latency).
4. Python turns that into per-field results, and performs the **government
   warning check deterministically in code** — see below.

### Why the browser orchestrates the batch

Each serverless invocation handles exactly one label, so a 300-label batch
can never hit a function timeout; results stream in per label instead of
blocking on the whole batch; and one bad image fails one row, not the run.
Concurrency is capped at 3 with a single retry on rate-limit responses,
because Gemini's free tier allows 15 requests/minute — a paid key raises
that limit and the same code simply runs wider.

### Key design decision: LLM for judgment, code for law

Two of the requirements pull in opposite directions:

- Brand-name matching needs *judgment*: `STONE'S THROW` on a label vs
  `Stone's Throw` in an application is obviously the same brand. The model
  is asked to judge equivalence, and its reasoning is surfaced as a note so
  the agent always sees *why*.
- The government warning (27 CFR 16.21) must be *exact*: verbatim wording,
  with `GOVERNMENT WARNING:` in capital letters. A legally exact requirement
  shouldn't depend on a model's judgment, so Gemini only transcribes the
  warning character-for-character; Python compares it to the canonical text
  (whitespace-normalized, case-sensitive) and separately checks the all-caps
  header. A title-case "Government Warning" is flagged as a mismatch.
  Boldness of the header can't be verified from a transcription, so the model
  reports a best-effort judgment; if it can't tell, the result says
  "verify visually" rather than silently passing.

### Performance

The prior vendor pilot failed because results took 30–40 s. This prototype
makes exactly one model call per label with thinking disabled; typical
end-to-end time is **2–4 seconds**, and the elapsed time is shown with every
result so slowness is never invisible.

## Run locally

```bash
git clone <repo>
cd label-checker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=...        # free key from https://aistudio.google.com
uvicorn api.index:app --reload
# open http://127.0.0.1:8000
```

## Deploy (Vercel)

1. Push this repo to GitHub and import it at vercel.com (framework: Other —
   `vercel.json` routes everything to the FastAPI app).
2. Add `GEMINI_API_KEY` under Project → Settings → Environment Variables.
3. Deploy. Done — no Docker, no build configuration.

## Testing it

Use **Download a sample CSV** in the UI, generate label images named to
match its `image_file` values, and upload both. Good adversarial tests:
wrong ABV on a label, a title-case "Government Warning", a missing warning
entirely, a glare-heavy photo, an image with no CSV row (skipped with a
warning), a CSV row with no image (flagged).

## Assumptions and trade-offs

- **Filename matching.** Rows and images are matched by filename
  (case-insensitive). This keeps the CSV format obvious for non-technical
  agents; a production system would match on COLA application IDs instead.
- **Free-tier rate limits.** At 15 requests/minute, a 300-label batch takes
  ~20 minutes on the free Gemini tier. The architecture is already parallel;
  a paid key (still pennies per label) makes the same batch take ~2 minutes.
- **Field set.** Brand name, class/type, alcohol content, net contents, and
  the warning — the core matching work agents described. Bottler
  name/address and country of origin would be added the same way.
- **Free-tier Gemini.** Google's free AI Studio tier may use submitted data
  for training; fine for synthetic test labels, not for production. A real
  deployment inside TTB's network would need a FedRAMP-authorized endpoint
  (e.g. Gemini on Google Cloud's Assured Workloads or an Azure-hosted
  model, given the agency's Azure footprint) — the model call is isolated
  in `api/verifier.py` precisely so the provider is swappable.
- **No auth or rate limiting.** Out of scope for a prototype that stores
  nothing and handles no PII. The endpoint validates content type and caps
  uploads at 4 MB.
- **Vercel serverless.** Chosen for zero-cost hosting with ~1 s cold starts
  (the prior pilot died on latency, so a host with 30 s+ cold starts was
  ruled out). Trade-off: no persistent process, so no server-side caching —
  acceptable at prototype volume.
- **Bold detection is best-effort.** Whether text is bold is a visual
  property a transcription can't prove; the tool flags uncertainty instead
  of guessing.

## Stack

FastAPI · Gemini 2.5 Flash (structured output) · vanilla HTML/JS ·
Vercel serverless. No frontend framework — the audience is compliance
agents, not developers, and the UI is a single page with two panels and one
button by design.
