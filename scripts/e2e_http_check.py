"""End-to-end check over REAL HTTP: uvicorn on a local port, the real embedding model, real sample files.

    python scripts/e2e_http_check.py

Phase A  runs the app exactly as configured with NO ANTHROPIC_API_KEY (generation unavailable).
Phase B  swaps in a STUB generator so the answered / refused / citation paths can be exercised without
         an API key. The stub is a TEST DOUBLE that lives in this script only. It is NOT Claude and
         says nothing about Claude's behaviour. Real Claude is covered by `pytest -m live`.
Exits non-zero if any check fails.
"""

from __future__ import annotations

import json
import re
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.rag.generation import GenerationResult  # noqa: E402

SAMPLES = ROOT / "samples"
FAILURES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


class StubGenerator:
    """TEST DOUBLE, not Claude. Cites the best passage verbatim; 'declines' for questions about facts
    the sample documents do not contain, imitating what a correctly behaving model should do."""

    available = True
    DECLINE = re.compile(r"\b(owns|owner|price|phone|battery|manufactured)\b", re.I)

    def generate(self, question, passages):
        if self.DECLINE.search(question):
            return GenerationResult(False, "The excerpts do not state that.", ())
        first = passages[0]
        return GenerationResult(True, f"[stub] {first.text.split('.')[0].strip()}. [{first.label}]", (first.label,))


def serve(generator, env, port):
    settings = Settings.from_env({**env, "RAG_MODEL_CACHE_DIR": str(ROOT / ".cache" / "models")})
    app = create_app(settings, generator=generator)  # real LazyOnnxEmbedder
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return server, thread


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(client: httpx.Client) -> dict:
    deadline = time.time() + 180
    while time.time() < deadline:
        r = client.get("/api/health")
        if r.status_code == 200:
            return r.json()
        time.sleep(0.5)
    raise SystemExit("server never became ready (embedding model failed to load?)")


def upload_folder(client, cid, folder: Path):
    files = [("files", (p.name, p.read_bytes(), "application/octet-stream")) for p in sorted(folder.iterdir())]
    return client.post(f"/api/collections/{cid}/documents", files=files)


def show_answer(label, body):
    print(f"     {label}: status={body['status']} grounded={body['grounded']} reason={body['reason']} "
          f"generator_called={body['generator_called']}")
    print(f"       answer: {body['answer']}")
    for c in body["citations"]:
        print(f"       cite [{c['label']}] {c['filename']} chunk {c['chunk_index']} score {c['score']} id {c['chunk_id']}")
        print(f"            “{c['snippet'][:90]}…”")


def phase_a() -> None:
    print("\n=== PHASE A: production wiring, NO ANTHROPIC_API_KEY ===")
    port = free_port()
    with tempfile.TemporaryDirectory() as data:
        server, thread = serve(None, {"RAG_DATA_DIR": data}, port)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as c:
                health = wait_ready(c)
                print("  health:", json.dumps({k: health[k] for k in ("status", "ready", "embedding", "generation")}))
                check(health["ready"] and health["generation"]["configured"] is False, "health: ready, generation NOT configured")
                ui = c.get("/")
                check(ui.status_code == 200 and "Content-Security-Policy" in ui.headers, "UI served with CSP over HTTP")

                cafe = c.post("/api/collections", json={"name": "Harbor Light Café"}).json()["id"]
                r = upload_folder(c, cafe, SAMPLES / "harbor_light_cafe")
                check(r.status_code == 201 and len(r.json()["indexed"]) == 2, "café documents uploaded and indexed", r.text)

                a = c.post(f"/api/collections/{cafe}/ask", json={"question": "What time does the café open on weekdays?"})
                e = a.json().get("error", {})
                check(a.status_code == 503 and e.get("code") == "generation_unavailable", "answerable question -> 503 generation_unavailable", a.text[:200])
                check(bool(e.get("evidence")) and e["evidence"][0]["filename"] == "staff_handbook.md", "…with the retrieved evidence attached")
                print("     evidence:", [(x["filename"], x["chunk_index"], x["score"]) for x in e.get("evidence", [])])
                check("answer" not in a.json() and "6:30" not in e.get("message", ""), "no answer was fabricated")

                u = c.post(f"/api/collections/{cafe}/ask", json={"question": "Who won the 1998 football world cup?"})
                check(u.status_code == 200 and u.json()["status"] == "insufficient_context" and not u.json()["generator_called"],
                      "unrelated question -> insufficient_context (no generator needed)")
        finally:
            server.should_exit = True
            thread.join(10)


def phase_b() -> None:
    print("\n=== PHASE B: real HTTP + real embeddings + STUB generator (a test double, not Claude) ===")
    port = free_port()
    with tempfile.TemporaryDirectory() as data:
        # A small upload limit so the oversize paths can be exercised with modest payloads.
        server, thread = serve(StubGenerator(), {"RAG_DATA_DIR": data, "RAG_MAX_UPLOAD_BYTES": "100000"}, port)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as c:
                health = wait_ready(c)
                check(health["generation"]["configured"] is True, "health: generation configured (stub)")

                ids, files = {}, {}
                for folder in sorted(p for p in SAMPLES.iterdir() if p.is_dir()):
                    ids[folder.name] = c.post("/api/collections", json={"name": folder.name}).json()["id"]
                    r = upload_folder(c, ids[folder.name], folder)
                    files[folder.name] = {p.name for p in folder.iterdir()}
                    check(r.status_code == 201 and not r.json()["rejected"], f"uploaded {folder.name}: {[d['filename'] for d in r.json()['indexed']]}")
                cafe, scope = ids["harbor_light_cafe"], ids["kestrel_telescope"]
                listing = c.get("/api/collections").json()["collections"]
                print("  collections:", [(x["name"], x["document_count"], x["chunk_count"]) for x in listing])
                docs = {n: {d["document_id"] for d in c.get(f"/api/collections/{i}/documents").json()["documents"]} for n, i in ids.items()}

                print("\n  -- answerable questions, same endpoint, two unrelated collections")
                for name, cid, question, expect in [
                    ("harbor_light_cafe", cafe, "What time does the café open on weekdays?", "6:30"),
                    ("kestrel_telescope", scope, "What is the focal length of the Kestrel-9?", "1200"),
                ]:
                    body = c.post(f"/api/collections/{cid}/ask", json={"question": question}).json()
                    show_answer(name, body)
                    cited = body["citations"]
                    check(body["status"] == "answered" and body["generator_called"], f"{name}: answered via the generator")
                    check(bool(cited) and all(x["filename"] in files[name] and x["document_id"] in docs[name] for x in cited),
                          f"{name}: every citation resolves to a document of THIS collection")
                    check(all(re.fullmatch(r"[0-9a-f]{32}:\d+", x["chunk_id"]) for x in cited) and expect in cited[0]["snippet"],
                          f"{name}: citation carries real chunk_id and the answering snippet")

                print("\n  -- insufficient context")
                body = c.post(f"/api/collections/{cafe}/ask", json={"question": "Explain how mRNA vaccines work."}).json()
                show_answer("unrelated", body)
                check(body["status"] == "insufficient_context" and body["reason"] == "below_threshold" and not body["generator_called"],
                      "unrelated question rejected by the gate before generation")
                body = c.post(f"/api/collections/{cafe}/ask", json={"question": "Who owns Harbor Light Café?"}).json()
                show_answer("related-unanswered", body)
                check(body["status"] == "insufficient_context" and body["reason"] == "model_declined" and body["generator_called"],
                      "on-topic but unanswerable question gets through the gate and is declined by the (stub) model")
                print("       NOTE: whether REAL Claude declines it is not tested here.")

                print("\n  -- collection isolation over HTTP (gate disabled, top_k=20)")
                for name, cid, question in [("harbor_light_cafe", cafe, "telescope mirror collimation magnification eyepiece"),
                                            ("kestrel_telescope", scope, "espresso loyalty stamps allergens pastry")]:
                    body = c.post(f"/api/collections/{cid}/ask", json={"question": question, "min_score": 0, "top_k": 20}).json()
                    seen = {e["filename"] for e in body["evidence"]}
                    check(bool(seen) and seen <= files[name], f"{name}: foreign question can only retrieve its own files", str(seen))
                for question in ("What time does the café open on weekdays?", "How many loyalty stamps earn a free coffee?",
                                 "Is there a nut allergy warning on the pastries?"):
                    body = c.post(f"/api/collections/{scope}/ask", json={"question": question}).json()
                    check(body["status"] == "insufficient_context" and not body["generator_called"],
                          f"café question asked of the telescope collection is gated: {question!r}",
                          f"top score {body['evidence'][0]['score']}")
                # Known limit, reported rather than hidden: a bag of unrelated keywords can land just above the gate.
                salad = c.post(f"/api/collections/{scope}/ask", json={"question": "espresso loyalty stamps pastry allergens"}).json()
                print(f"     (info) keyword-salad café query vs telescope: top score {salad['evidence'][0]['score']} vs threshold "
                      f"{salad['threshold']} -> {salad['status']}; evidence files stayed in-collection: "
                      f"{ {e['filename'] for e in salad['evidence']} <= files['kestrel_telescope'] }")

                print("\n  -- upload edge cases over HTTP")
                post = lambda files_: c.post(f"/api/collections/{cafe}/documents", files=files_)  # noqa: E731
                for label, f, status, code in [
                    ("unsupported .exe", ("x.exe", b"MZ", "application/octet-stream"), 415, "unsupported_document"),
                    ("empty file", ("e.txt", b"", "text/plain"), 422, "empty_document"),
                    ("oversized file (150 kB > 100 kB)", ("big.txt", b"word " * 30_000, "text/plain"), 413, "document_too_large"),
                ]:
                    r = post([("files", f)])
                    check(r.status_code == status and r.json()["rejected"][0]["code"] == code, f"{label} -> {status} {code}", r.text[:120])
                r = post([("files", ("huge.txt", b"a" * 3_000_000, "text/plain"))])
                check(r.status_code == 413 and r.json()["error"]["code"] == "request_too_large", "3 MB body -> 413 request_too_large (refused while streaming)")
                check(c.get("/api/collections").json()["collections"][0]["chunk_count"] > 0, "server still healthy after oversize attempts")

                print("\n  -- hostile filename + content")
                hostile_name = "<img src=x onerror=alert(1)>.txt"
                r = c.post(f"/api/collections/{cafe}/documents", files=[("files", (hostile_name, b"<script>alert(1)</script> The staff handbook says tips are pooled.", "text/html"))])
                check(r.status_code == 201 and r.json()["indexed"][0]["filename"] == hostile_name, "hostile filename accepted as inert text")
                body = c.post(f"/api/collections/{cafe}/ask", json={"question": "How is the tip money shared out among employees?"})
                check(body.headers["content-type"].startswith("application/json") and body.headers["x-content-type-options"] == "nosniff",
                      "responses are JSON + nosniff (never HTML)")
                print("       citation filenames:", [x["filename"] for x in body.json()["citations"]])

                print("\n  -- deletion")
                check(c.delete(f"/api/collections/{scope}").status_code == 204, "DELETE telescope collection -> 204")
                check(c.post(f"/api/collections/{scope}/ask", json={"question": "focal length"}).status_code == 404, "asking the deleted collection -> 404")
                check(c.post(f"/api/collections/{cafe}/ask", json={"question": "What time does the café open on weekdays?"}).json()["status"] == "answered",
                      "the other collection is unaffected")
        finally:
            server.should_exit = True
            thread.join(10)


if __name__ == "__main__":
    phase_a()
    phase_b()
    print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"{len(FAILURES)} CHECK(S) FAILED:\n  - " + "\n  - ".join(FAILURES)))
    raise SystemExit(1 if FAILURES else 0)
