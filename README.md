# RAG Generator

Upload documents at runtime, get a retrieval-augmented question-answering application over exactly
those documents, and ask questions that are answered **only from what you uploaded** — with source
citations, and an explicit "insufficient context" response when the documents do not contain the answer.

Built for the *Agentic Coding Assessment* (Option A: RAG Generator).

| Assessment requirement | How it is met |
|---|---|
| Accepts documents at runtime | `POST /api/collections/{id}/documents` and the web UI accept `.txt .md .html .pdf .docx` uploads (or pasted text). Nothing is baked into the code. |
| Creates a RAG application over them | Each upload is extracted, chunked, embedded locally, and added to that collection's own vector index. A *collection* is the RAG application. |
| Grounded answers | Retrieve → confidence gate → Claude answers from the retrieved excerpts only → citations are validated. See [Grounding](#grounding-and-citations). |
| Different document sets, no code changes | Create another collection and upload other files through the same endpoints. Two unrelated sample sets (`samples/`) go through one running server in `scripts/e2e_http_check.py`; the API tests do the same with other document sets on a single app instance. |

Also: source citations, an explicit insufficient-context response, strict collection isolation,
automated tests for ingestion/retrieval/grounding/isolation/API, no hard-coded document content, and
secrets kept out of git.

## Quick start

Requires Python 3.11+ and network access on first run (to download the ~83 MB embedding model once). Tested on Python 3.11 and 3.13; dependency ranges in `pyproject.toml` are bounded to the tested major versions (there is deliberately no lock file).

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"              # runtime only: pip install -e .

export ANTHROPIC_API_KEY=sk-ant-...  # optional, see below; without it retrieval works but answers do not
uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/> (UI) or <http://127.0.0.1:8000/docs> (interactive API docs; Swagger UI loads its assets from a CDN, so the browser needs internet access for that page).
The embedding model downloads in the background at startup; `/api/health` reports `"status": "starting"`
(HTTP 503) until it is loaded.

### Configuring `ANTHROPIC_API_KEY` securely

Generation uses the Anthropic API and needs a key from <https://console.anthropic.com/>.

* The key is read **only from the environment variable `ANTHROPIC_API_KEY`**. It is never written to
  disk by the app, never logged, never returned by any endpoint, and hidden from `repr()` of the settings object.
* Do not commit it. `.env` is git-ignored. If you prefer a file, copy `.env.example` to `.env`, fill it in, and load
  it into your shell yourself: `set -a; source .env; set +a`. (The app deliberately does not auto-load `.env`.)
* **Without a key** the app still runs: you can create collections, upload and search documents, and see the retrieved
  passages. Asking a question that needs an answer returns HTTP 503 `generation_unavailable` together with the
  retrieved evidence, and the UI shows a clear "Claude generation is NOT configured" banner. There is
  no silent fallback to extractive answers or to another model.
* The default model is `claude-opus-5-5`; override with `ANTHROPIC_MODEL`.
* Binding to `0.0.0.0` exposes an unauthenticated service. Keep the default `127.0.0.1` unless you add your own protection.

## Using it

### Web UI

1. **Create** a collection and select it. 2. **Upload** files (or paste text); each file shows as indexed or
rejected with a reason. 3. **Ask** a question. You get one of: a *Grounded answer* with its cited sources, an
*Insufficient context* result (with the reason), *Answer withheld*, or *Claude generation is not configured*
(with the retrieved passages). "Retrieved evidence" lists every passage that was found, its score, and whether it was
sent to Claude.

### API

| Method & path | Purpose |
|---|---|
| `POST /api/collections` `{"name": "..."}` | Create a collection → `201` |
| `GET /api/collections` | List collections with document/chunk counts |
| `DELETE /api/collections/{id}` | Delete a collection and its data → `204` |
| `POST /api/collections/{id}/documents` (multipart, field `files`, ≤5 files) | Upload and index → `201` with `indexed` / `rejected` lists |
| `GET /api/collections/{id}/documents` | List indexed documents |
| `POST /api/collections/{id}/ask` `{"question": "...", "top_k"?: 1-20, "min_score"?: 0-1}` | Ask |
| `GET /api/health` | Readiness: embedding model state, whether generation is configured, active limits |

```bash
BASE=http://127.0.0.1:8000
CID=$(curl -s -X POST $BASE/api/collections -H 'content-type: application/json' \
      -d '{"name":"cafe"}' | python -c 'import sys,json; print(json.load(sys.stdin)["id"])')
curl -s -X POST $BASE/api/collections/$CID/documents \
     -F files=@samples/harbor_light_cafe/staff_handbook.md -F files=@samples/harbor_light_cafe/customer_policies.txt
curl -s -X POST $BASE/api/collections/$CID/ask -H 'content-type: application/json' \
     -d '{"question":"What time does the café open on weekdays?"}'
```

An answer looks like this (abridged):

```json
{
  "status": "answered", "grounded": true, "generator_called": true, "threshold": 0.15,
  "answer": "The café opens at 6:30 a.m. on weekdays [S1].",
  "citations": [{"label": "S1", "chunk_id": "9f2c…:0", "document_id": "9f2c…", "filename": "staff_handbook.md",
                 "chunk_index": 0, "snippet": "# Harbor Light Café — Staff Handbook …", "score": 0.5039}],
  "evidence":  [{"chunk_id": "9f2c…:0", "filename": "staff_handbook.md", "score": 0.5039, "label": "S1", "used": true, "…": "…"}]
}
```

`status` is `answered`, `insufficient_context` (with `reason`: `empty_collection`, `below_threshold`, or
`model_declined`), or `unverified` (`no_valid_citations`). HTTP errors use `{"error": {"code", "message"}}`:
`404 collection_not_found`, `415 unsupported_document`, `413 document_too_large` / `request_too_large`,
`422 invalid_input` / `empty_document` / `unreadable_document` / `invalid_request`, `409 embedder_mismatch`,
`503 generation_unavailable` / `embedding_unavailable`, `502 generation_failed`, `500 storage_corruption` /
`storage_error` / `internal_error`. Server-side messages are fixed strings; paths, URLs, keys and stack traces go only to the server log.

### Supported documents

`.txt`, `.md`/`.markdown`, `.html`/`.htm` (scripts and styles stripped), `.pdf` (text layer only — **no OCR**; encrypted or
image-only PDFs are rejected), `.docx` (paragraphs and tables). Default limits: 10 MiB per file, 5 files per request.

## Architecture

```
 browser / curl ──► FastAPI (app/api, app/main.py)   thin: validation, error mapping, size caps, security headers
                         │
                         ▼
                   AnswerService (app/rag/answering.py)      retrieve → gate → generate → validate
                    │                      │
                    ▼                      ▼
            CollectionManager         Generator (app/rag/generation.py)
   loaders → chunking → Embedder          AnthropicGenerator  |  UnavailableGenerator (no key)
   → per-collection VectorStore (NumPy)
        data/collections/<id>/{meta.json, index.npz}
```

**Upload → index.** The route reads each file with a bounded read, `load_text` extracts text by extension, the chunker
splits it (800 characters, 150 overlap, preferring paragraph/sentence/word boundaries), the embedder turns each chunk
into a unit vector, and the collection commits the new chunks to its index atomically.

**Question → answer.**
1. Embed the question and retrieve the top-k chunks from **that collection only**.
2. *Retrieval-confidence gate:* if no chunk scores ≥ the threshold, return a fixed "insufficient context" result
   **without calling Claude**.
3. Otherwise send Claude only the passages that passed the gate, labelled `S1`, `S2`, …, plus the question.
4. Claude must reply with JSON `{grounded, answer, citations}`. It may itself decline (`grounded: false`).
5. The application validates the reply and returns the structured result.

Everything under `app/rag/` is independent of FastAPI, so the pipeline is tested without HTTP.

### Why these choices

* **ONNX all-MiniLM-L6-v2 for embeddings.** Real semantic retrieval that runs locally on CPU with no paid embedding API and no
  PyTorch (`onnxruntime` + `tokenizers` only; 384 dimensions). The archive is downloaded once, **SHA-256 verified**
  (`913d7300…`, computed from the actual download), and extracted with an allow-list of two file names. If it cannot
  be obtained the app says so; it never falls back to keyword search.
* **Per-collection NumPy store.** Exact cosine search is instant at assessment scale, adds no dependency or hidden model
  download (unlike Chroma/FAISS), and makes isolation structural: a collection *is* its own matrix on its own directory.
* **Claude for generation.** Strong instruction-following for "answer only from these excerpts, cite them, or say you can't",
  with schema-constrained output. Wrapped behind a small `Generator` interface so tests use a fake and the real call is isolated.

## Grounding and citations

* Claude receives only the retrieved excerpts and the question, and is instructed to use nothing else, to treat the excerpts as
  untrusted data, and to set `grounded: false` when the excerpts are on-topic but do not state the answer.
* **Two refusal layers.** The retrieval gate catches clearly unrelated questions deterministically. Claude's own refusal
  catches on-topic questions the documents cannot answer, which similarity scores cannot distinguish (see calibration).
  When Claude declines, the response is the fixed insufficient-context text, never the model's free text (its note is
  returned separately as `detail`).
* **Citations are validated by the application, not trusted.** Model-facing labels are short (`S1`, `S2`); each response
  returns the real `chunk_id`, `document_id`, filename, chunk index, snippet and score. Only labels that were actually sent
  are accepted; invented or malformed ones are dropped and markers stripped from the text; an answer with no valid citation
  is withheld (`unverified`). Validation proves a cited passage *exists and was retrieved*, not that it entails the claim.

### The `0.15` retrieval threshold

Chosen from a small calibration run (`scripts/calibrate_threshold.py`, data in `samples/calibration.json`: 58 queries over
the two sample collections, real embedding model). Top-1 cosine score per category:

| Category | n | min | median | max |
|---|---|---|---|---|
| answerable | 12 | 0.141 | 0.456 | 0.706 |
| paraphrased | 8 | 0.316 | 0.355 | 0.553 |
| exact-term (1–4 word queries) | 8 | 0.115 | 0.311 | 0.435 |
| **related but unanswered** | 14 | **0.273** | 0.401 | **0.614** |
| unrelated | 16 | −0.028 | 0.036 | 0.173 |

| Gate threshold | answerable | paraphrased | exact-term | related-unanswered | unrelated |
|---|---|---|---|---|---|
| 0.10 | 12/12 | 8/8 | 8/8 | 14/14 | 4/16 pass |
| **0.15** | 11/12 | 8/8 | 6/8 | 14/14 | **1/16 pass** |
| 0.20 | 11/12 | 8/8 | 5/8 | 14/14 | 0/16 pass |
| 0.30 | 10/12 | 8/8 | 4/8 | 12/14 | 0/16 pass |

At 0.15 the gate rejects 15 of 16 unrelated questions and keeps 25 of 28 legitimate ones. Because Claude is a second
layer, the gate is tuned not to reject real questions. **Limitations — read before trusting it:**

* It is corpus- and model-dependent and the calibration set is small (two collections of 3–4 chunks). Larger corpora raise the
  background similarity of unrelated queries, so **recalibrate for any real deployment**
  (`python scripts/calibrate_threshold.py --verbose`). It is configurable: `RAG_MIN_SCORE` or `min_score` per request.
* On-topic-but-unanswerable questions score exactly like answerable ones (0.27–0.61 vs 0.14–0.71); no threshold separates them.
  That case relies entirely on Claude declining.
* Very short keyword queries score low (`collimation` = 0.115) and can be wrongly gated; keyword lists can land just above the gate.
* Only passages scoring ≥ threshold are sent, so a relevant second chunk just below it can be dropped.

## Collection isolation

Every operation resolves a collection id to *that collection's* directory and in-memory index. There is no shared index and no
metadata filter that could be forgotten. Ids are random 32-hex UUIDs validated by regex; anything else (including path traversal)
is a plain 404 and never touches the filesystem. The tests ask each collection about the other's topic with the gate disabled and a
large `top_k` and assert that nothing foreign is retrieved, sent to Claude, or returned — through the domain layer, the full
answer flow, and HTTP.

## Persistence

State lives under `data/` (git-ignored): `collections/<id>/meta.json` and `index.npz` (vectors **and** chunk metadata in one file).

* An upload builds the next immutable index, writes it to a temp file, `fsync`s, then atomically `os.replace`s it, and **only then**
  publishes it in memory. A failed write leaves both disk and the visible state unchanged. With a single file there is no window
  where vectors and metadata disagree after a crash.
* Loading validates format version, CRC, shapes, dimensions, finiteness and field types; damage raises a distinct
  `StorageCorruptionError` (HTTP 500 `storage_corruption`). A collection can still be deleted so it can be re-created.
* A collection records which embedder built it; opening it with a different one is refused (`409`).
* Concurrency: one lock **per collection**, held only for the short commit; embedding runs outside any lock, so slow uploads do
  not block queries or other collections, and concurrent uploads to one collection lose nothing. Queries read an immutable snapshot.

## Security

* **Prompt injection.** Rules live in the system prompt, which contains no document text. Excerpts, filenames and the question are
  XML-escaped inside delimited `<excerpt>`/`<question>` elements, so a document cannot close its own element or forge a label; the
  model has no tools and must answer in a fixed JSON schema; citations are validated by code. This cannot make injection impossible
  — a document can still try to bend the answer *wording* — but it cannot forge citations or break out of the data region. The tests
  include a document that tries to override the instructions, forge `[S9]`, and inject tags.
* **No executable HTML.** The UI inserts all dynamic text with `textContent`/`append(string)` (a test fails if `innerHTML` and
  friends appear in the source), uses no inline scripts, and every response carries a strict Content-Security-Policy
  (`script-src 'self'`, no `unsafe-inline`/`unsafe-eval`), `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY`.
  Filenames are reduced to a display name and never used as paths.
* **Upload limits** are enforced while the body streams (declared or actual size over the cap is cut off before parsing), plus a
  bounded per-file read, a 64 KiB cap on JSON bodies, a 2,000-character question limit, and a safe tar extraction for the model archive.
* **Error hygiene** and **key handling** as described above.

## Tests

```bash
pytest                                   # default: 270 offline, deterministic tests, no network, no model, no API key
pytest -m "model and not live"           # 15 tests with the REAL embedding model (downloads once into .cache/models)
ANTHROPIC_API_KEY=... pytest -m live -v  # 4 tests that call the REAL Claude API; costs money
python scripts/e2e_http_check.py         # real uvicorn + real model + real HTTP; uses a clearly-labelled STUB generator
```

* The offline suite uses deterministic fakes (a concept-bag embedder and a scriptable generator), blocks all socket connections, and
  covers ingestion, chunking, retrieval, grounding/refusal, citation validation, injection handling, isolation, concurrency,
  atomic persistence, corruption detection, and the HTTP API/UI. A test asserts the default run excludes `model`/`live` tests.
* The `model` suite checks real semantic retrieval, real isolation on the sample sets, and the calibration thresholds.
* **The `live` tests were written but have not been run in the development environment** (no API key was available there). Everything
  about the real Claude call — request shape, error mapping, response parsing — is tested against fakes and the real SDK talking to an
  in-process mock server, but not against the live API. Run `pytest -m live` once with a key.

## Repository layout

```
app/main.py            app factory, error handlers        app/rag/loaders.py      text extraction
app/api/               routes, schemas, errors, middleware app/rag/chunking.py     boundary-aware chunking
app/static/            the UI (index.html, app.js, css)    app/rag/embeddings.py   ONNX embedder, model download/verify
app/config.py          env-driven settings                 app/rag/store.py        NumPy vector store, atomic persistence
app/rag/answering.py   gate → generate → validate          app/rag/collections.py  isolated collections, locking
app/rag/generation.py  Generator, Claude implementation    tests/  scripts/  samples/  .env.example
```

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(unset)* | Enables generation; blank counts as unset |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude model used for answers |
| `RAG_MIN_SCORE` | `0.15` | Retrieval-confidence threshold (0–1) |
| `RAG_TOP_K` | `4` | Passages retrieved per question |
| `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` | `800` / `150` | Characters; ~200 tokens fits MiniLM's 256-token window |
| `RAG_MAX_UPLOAD_BYTES` | `10485760` | Per-file size limit |
| `RAG_DATA_DIR` | `data` | Where collections are stored |
| `RAG_MODEL_CACHE_DIR` | `.cache/models` | Where the embedding model is cached |
| `RAG_MODEL_URL` / `RAG_MODEL_SHA256` | Chroma mirror / pinned digest | Change both together |

## Known limitations

* **Single process.** Locks are in-process; two server processes on one `data/` directory are not supported.
* **Whole-index rewrite per upload** (and a full matrix copy), so very large collections get slower to ingest. A crash can leave an
  orphaned `index.npz.tmp` (harmless; overwritten by the next save).
* **No authentication, rate limiting or quotas**; anyone who can reach the server can create collections and fill the disk.
  The request-size cap is generous by default (~51 MB per upload request).
* **Retrieval:** dense-only, English-oriented model, no reranking or keyword matching, so exact-term and very short queries are weak,
  and non-English documents retrieve poorly. Scores are not calibrated across corpora (see above).
* **Grounding is probabilistic.** Citation validation checks existence, not entailment; a model could cite a real passage for an
  unsupported claim, or cite the wrong valid passage. Declining on-topic-but-unanswerable questions depends on Claude (live-test it).
* No OCR, no per-document deletion (delete the collection), no multi-turn conversation memory, no streaming responses.
* `list_collections` skips damaged or embedder-incompatible collections (logged) instead of showing them with an error.

**Sensible next steps:** hybrid dense+keyword retrieval and a reranker; an entailment check on cited claims; an authenticated,
multi-user deployment with quotas; a real vector store and cross-process locking for large corpora; OCR; incremental index updates;
streaming answers.

## AI-assisted development

This project was built with an AI coding agent (Claude Code) **on purpose**: it is an *Agentic Coding Assessment*, and the
point is to show how an agent can be directed to deliver working, tested software. The work proceeded in reviewed slices — domain layer,
storage/concurrency hardening, grounded generation with threshold calibration, then the API and UI — each followed by human review,
adversarial testing, and deliberate breakage of the code to confirm the tests catch regressions.

The **complete agent transcript is in this repository** under `transcript/`:

* `agent_session.jsonl` — the unedited Claude Code session log and the source of truth: every prompt, reply, tool call and tool result.
* `agent_session_readable.md` — a mechanical Markdown rendering of that log for easier reading, with no redaction, truncation or
  paraphrase (pure bookkeeping entries such as token counters exist only in the `.jsonl`).

Both files were captured immediately before the commit that added them, so they end just before that commit. They are unredacted and
therefore contain local file paths and account identifiers.
