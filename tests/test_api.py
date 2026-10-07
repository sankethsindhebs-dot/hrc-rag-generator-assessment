"""HTTP API tests (in-process, fully offline): the complete flow plus every error mapping."""

import json
import re

import pytest
from fastapi.testclient import TestClient

import app.rag.store as store_module
from app.config import Settings
from app.main import create_app
from app.rag.collections import CollectionManager
from app.rag.embeddings import LazyOnnxEmbedder
from app.rag.errors import GenerationError, ModelUnavailableError
from app.rag.generation import UnavailableGenerator
from tests.helpers import FakeEmbedder, FakeGenerator, cites

CARS = b"The automobile engine in this car needs service. A truck is a vehicle too."
FRUIT = b"An apple orchard grows pear and banana fruit every year."
# Ids that reach the collection routes and must be reported as plain "not found" by the domain layer.
BOGUS_IDS = ["%2e%2e", "x" * 32, "0" * 31, "0" * 33, "0" * 32, "a b", "%00", "G" * 32, "..%00"]
# Path-traversal attempts never even match a collection route (the router normalises or rejects them).
TRAVERSAL_IDS = ["../etc", "..%2F..%2Fetc", "%2e%2e%2f%2e%2e%2fetc", "....//....//etc"]


def citing_generator():
    return FakeGenerator(lambda q, passages: cites(f"{passages[0].text[:50]} [{passages[0].label}]", passages[0].label))


@pytest.fixture
def make_client(settings, embedder):
    def make(generator=None, *, embedder_=None, settings_=None):
        app = create_app(settings_ or settings, embedder=embedder_ or embedder, generator=generator or citing_generator())
        return TestClient(app, raise_server_exceptions=False)
    return make


@pytest.fixture
def client(make_client):
    return make_client()


def create(client, name="c"):
    response = client.post("/api/collections", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client, cid, *files):
    return client.post(f"/api/collections/{cid}/documents", files=[("files", f) for f in files])


def ask(client, cid, question, **extra):
    return client.post(f"/api/collections/{cid}/ask", json={"question": question, **extra})


def error_of(response):
    return response.json()["error"]


# ---- the complete assessment flow ----------------------------------------------

def test_two_unrelated_collections_through_the_same_endpoints_without_any_change(client):
    cars, fruit = create(client, "cars"), create(client, "fruit")
    r = upload(client, cars, ("cars.txt", CARS, "text/plain"))
    assert r.status_code == 201 and r.json()["indexed"][0]["filename"] == "cars.txt" and r.json()["rejected"] == []
    assert upload(client, fruit, ("fruit.md", FRUIT, "text/markdown")).status_code == 201

    listing = {c["name"]: c for c in client.get("/api/collections").json()["collections"]}
    assert listing["cars"]["document_count"] == 1 and listing["fruit"]["chunk_count"] >= 1
    docs = client.get(f"/api/collections/{fruit}/documents").json()["documents"]
    assert [d["filename"] for d in docs] == ["fruit.md"]

    a = ask(client, cars, "automobile engine")
    assert a.status_code == 200
    body = a.json()
    assert (body["status"], body["grounded"], body["generator_called"]) == ("answered", True, True)
    assert body["answer"].endswith("[S1]")
    citation = body["citations"][0]
    assert set(citation) == {"label", "chunk_id", "document_id", "filename", "chunk_index", "snippet", "score"}
    assert (citation["label"], citation["filename"], citation["chunk_index"]) == ("S1", "cars.txt", 0)
    assert re.fullmatch(r"[0-9a-f]{32}:\d+", citation["chunk_id"]) and "automobile" in citation["snippet"]
    assert body["evidence"][0]["used"] is True and body["evidence"][0]["chunk_id"] == citation["chunk_id"]

    b = ask(client, fruit, "apple orchard").json()
    assert b["status"] == "answered" and b["citations"][0]["filename"] == "fruit.md"

    # Asking each collection about the OTHER one's topic: nothing crosses over, anywhere in the response.
    for cid, question, foreign in [(cars, "apple orchard banana", "apple"), (fruit, "automobile engine truck", "automobile")]:
        r = ask(client, cid, question)
        assert r.status_code == 200 and r.json()["status"] == "insufficient_context"
        returned = json.dumps({k: v for k, v in r.json().items() if k != "question"}).lower()  # the question is the caller's own text
        assert foreign not in returned and r.json()["evidence"], "only this collection's own passages may come back"


def test_insufficient_context_is_explicit_and_skips_the_generator(make_client):
    generator = citing_generator()
    client = make_client(generator)
    cid = create(client)
    upload(client, cid, ("cars.txt", CARS, "text/plain"))
    r = ask(client, cid, "zebra giraffe")
    body = r.json()
    assert r.status_code == 200
    assert (body["status"], body["grounded"], body["reason"], body["generator_called"]) == (
        "insufficient_context", False, "below_threshold", False)
    assert body["citations"] == [] and "could not find enough information" in body["answer"]
    assert body["evidence"] and all(e["used"] is False for e in body["evidence"]) and body["threshold"] == 0.15
    assert generator.calls == []


def test_model_declining_is_reported_as_insufficient_context(make_client):
    client = make_client(FakeGenerator(cites("It does not say.", grounded=False)))
    cid = create(client)
    upload(client, cid, ("cars.txt", CARS, "text/plain"))
    body = ask(client, cid, "Does the automobile have a sunroof?").json()
    assert (body["status"], body["reason"], body["grounded"]) == ("insufficient_context", "model_declined", False)
    assert body["detail"] == "It does not say."


def test_ask_options_are_passed_through(client):
    cid = create(client)
    upload(client, cid, ("cars.txt", CARS, "text/plain"))
    assert ask(client, cid, "car apple", min_score=0.9).json()["reason"] == "below_threshold"
    assert ask(client, cid, "car apple", min_score=0.5).json()["status"] == "answered"
    assert len(ask(client, cid, "car", top_k=1).json()["evidence"]) == 1


# ---- collection ids and missing collections ------------------------------------

def every_collection_route(client, bad):
    base = f"/api/collections/{bad}"
    return [
        client.get(f"{base}/documents"),
        client.delete(base),
        client.post(f"{base}/ask", json={"question": "hi"}),
        client.post(f"{base}/documents", files=[("files", ("a.txt", b"hello", "text/plain"))]),
    ]


@pytest.mark.parametrize("bad", BOGUS_IDS)
def test_malformed_or_unknown_collection_ids_are_a_plain_404_on_every_route(client, bad):
    for response in every_collection_route(client, bad):
        assert response.status_code == 404, (response.request.method, response.request.url, response.text)
        assert error_of(response) == {"code": "collection_not_found", "message": "Collection not found."}


@pytest.mark.parametrize("bad", TRAVERSAL_IDS)
def test_path_traversal_ids_are_refused_and_touch_nothing(client, settings, bad):
    keep = create(client, "keep")
    upload(client, keep, ("cars.txt", CARS, "text/plain"))
    for response in every_collection_route(client, bad):
        assert response.status_code in (404, 405), (response.request.method, response.request.url, response.text)
    assert client.get(f"/api/collections/{keep}/documents").json()["documents"][0]["filename"] == "cars.txt"  # nothing was deleted


def test_missing_collection_never_triggers_embedding_or_generation(make_client, embedder):
    generator = citing_generator()
    client = make_client(generator)
    assert upload(client, "0" * 32, ("a.txt", b"hello car", "text/plain")).status_code == 404
    assert ask(client, "0" * 32, "car").status_code == 404
    assert embedder.calls == [] and generator.calls == []


# ---- uploads -------------------------------------------------------------------

def test_unsupported_empty_and_unreadable_uploads_are_rejected_cleanly(client):
    cid = create(client)
    cases = {
        ("malware.exe", b"MZ\x90\x00", "application/octet-stream"): (415, "unsupported_document"),
        ("noextension", b"data", "text/plain"): (415, "unsupported_document"),
        ("empty.txt", b"", "text/plain"): (422, "empty_document"),
        ("blank.md", b"  \n\t ", "text/markdown"): (422, "empty_document"),
        ("binary.txt", b"abc\x00def", "text/plain"): (422, "unreadable_document"),
        ("broken.pdf", b"%PDF-1.4 not really", "application/pdf"): (422, "unreadable_document"),
        ("broken.docx", b"not a zip", "application/octet-stream"): (422, "unreadable_document"),
    }
    for file, (status, code) in cases.items():
        r = upload(client, cid, file)
        assert r.status_code == status, (file[0], r.status_code, r.text)
        assert r.json()["indexed"] == [] and r.json()["rejected"][0]["code"] == code
        assert r.json()["rejected"][0]["filename"] == file[0]
    assert client.get(f"/api/collections/{cid}/documents").json()["documents"] == []


def test_oversized_document_is_rejected_per_file(client):
    cid = create(client)
    r = upload(client, cid, ("big.txt", b"a" * 50_001, "text/plain"))
    assert r.status_code == 413 and r.json()["rejected"][0]["code"] == "document_too_large"
    assert upload(client, cid, ("max.txt", b"a" * 50_000, "text/plain")).status_code == 201  # exactly at the limit


def test_a_mixed_upload_indexes_the_good_files_and_reports_the_bad(client):
    cid = create(client)
    r = upload(client, cid, ("good.txt", CARS, "text/plain"), ("bad.exe", b"x", "application/octet-stream"),
               ("empty.txt", b"", "text/plain"))
    body = r.json()
    assert r.status_code == 201
    assert [d["filename"] for d in body["indexed"]] == ["good.txt"]
    assert {x["filename"]: x["code"] for x in body["rejected"]} == {"bad.exe": "unsupported_document", "empty.txt": "empty_document"}


def test_upload_with_no_files_or_too_many_files(client, embedder):
    cid = create(client)
    assert client.post(f"/api/collections/{cid}/documents").status_code == 422
    six = [("a%d.txt" % i, CARS, "text/plain") for i in range(6)]
    r = upload(client, cid, *six)
    assert r.status_code == 422 and error_of(r)["code"] == "invalid_input"
    assert client.get(f"/api/collections/{cid}/documents").json()["documents"] == []
    assert embedder.calls == []


def test_oversized_request_body_is_refused_before_being_parsed(client, embedder):
    cid = create(client)
    huge = b"a" * (2 * 1024 * 1024)  # far beyond 5 files x 50,000 bytes + overhead
    r = upload(client, cid, ("huge.txt", huge, "text/plain"))
    assert r.status_code == 413 and error_of(r)["code"] == "request_too_large"
    assert embedder.calls == []


def test_oversized_body_without_content_length_is_cut_off_while_streaming(client):
    """Chunked upload (no Content-Length): the cap must still apply as the bytes arrive."""
    cid = create(client)
    boundary = "b0undary"
    chunk = b"x" * 65_536
    sent = []

    def body():
        yield f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\n'.encode()
        for _ in range(100):  # would be 6.5 MB
            sent.append(1)
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    r = client.post(f"/api/collections/{cid}/documents", content=body(),
                    headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    assert r.status_code == 413 and error_of(r)["code"] == "request_too_large"


def test_large_json_bodies_are_refused(client):
    r = client.post("/api/collections", content=b'{"name": "' + b"a" * 70_000 + b'"}', headers={"content-type": "application/json"})
    assert r.status_code == 413 and error_of(r)["code"] == "request_too_large"


def test_uploaded_filenames_are_reduced_to_a_display_name(client):
    cid = create(client)
    r = upload(client, cid, ("../../etc/passwd.txt", CARS, "text/plain"), ("C:\\temp\\win.txt", FRUIT, "text/plain"))
    assert sorted(d["filename"] for d in r.json()["indexed"]) == ["passwd.txt", "win.txt"]


# ---- validation / routing ------------------------------------------------------

@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/collections", {}),
        ("/api/collections", {"name": "x", "evil": 1}),
        ("/api/collections", {"name": 5}),
        ("/api/collections/ID/ask", {"question": "q", "top_k": 0}),
        ("/api/collections/ID/ask", {"question": "q", "top_k": 21}),
        ("/api/collections/ID/ask", {"question": "q", "min_score": 1.5}),
        ("/api/collections/ID/ask", {"top_k": 3}),
    ],
)
def test_malformed_requests_get_a_clean_422(client, path, payload):
    cid = create(client)
    r = client.post(path.replace("ID", cid), json=payload)
    assert r.status_code == 422 and error_of(r)["code"] == "invalid_request"
    assert all(set(d) == {"field", "message"} for d in error_of(r)["details"])  # no echoed input


def test_domain_validation_errors_are_422(client):
    cid = create(client)
    for name in ("", "   ", "x" * 101):
        assert client.post("/api/collections", json={"name": name}).status_code == 422
    for question in ("", "  ", "q" * 2001):
        r = ask(client, cid, question)
        assert r.status_code == 422 and error_of(r)["code"] == "invalid_input"
    assert client.post("/api/collections", content=b"not json", headers={"content-type": "application/json"}).status_code == 422


def test_unknown_routes_and_methods_use_the_error_envelope(client):
    assert client.get("/api/nope").status_code == 404 and error_of(client.get("/api/nope"))["code"] == "not_found"
    r = client.put("/api/collections")
    assert r.status_code == 405 and error_of(r)["code"] == "method_not_allowed"


# ---- generation not configured -------------------------------------------------

def test_missing_generation_config_is_explicit_and_retrieval_still_works(settings, embedder):
    assert settings.anthropic_api_key is None
    client = TestClient(create_app(settings, embedder=embedder), raise_server_exceptions=False)  # builds the real generator factory
    health = client.get("/api/health")
    assert health.status_code == 200 and health.json()["ready"] is True
    assert health.json()["generation"]["configured"] is False and "ANTHROPIC_API_KEY" in health.json()["generation"]["detail"]

    cid = create(client)
    upload(client, cid, ("cars.txt", CARS, "text/plain"))
    r = ask(client, cid, "automobile engine")
    assert r.status_code == 503 and error_of(r)["code"] == "generation_unavailable"
    assert "ANTHROPIC_API_KEY" in error_of(r)["message"]
    evidence = error_of(r)["evidence"]
    assert evidence and evidence[0]["filename"] == "cars.txt" and evidence[0]["used"] is True
    assert "answer" not in r.json() and "answer" not in r.json()["error"]  # nothing was fabricated

    # A question the gate rejects needs no generator, so it still gets the normal refusal.
    unrelated = ask(client, cid, "zebra giraffe")
    assert unrelated.status_code == 200 and unrelated.json()["status"] == "insufficient_context"


def test_generation_failures_map_to_502_without_leaking_detail(make_client):
    client = make_client(FakeGenerator(GenerationError("Anthropic rate limit reached; try again shortly.")))
    cid = create(client)
    upload(client, cid, ("cars.txt", CARS, "text/plain"))
    r = ask(client, cid, "automobile engine")
    assert r.status_code == 502 and error_of(r)["code"] == "generation_failed"


# ---- storage / model / unexpected failures -------------------------------------

def no_internals(response, settings):
    text = response.text
    assert str(settings.data_dir) not in text and "index.npz" not in text and "Traceback" not in text
    assert "/home/" not in text and "sk-ant" not in text and ".py" not in text


def test_storage_corruption_maps_to_500_with_a_generic_message(settings, embedder, make_client):
    first = make_client()
    cid = create(first)
    upload(first, cid, ("cars.txt", CARS, "text/plain"))
    index = settings.data_dir / "collections" / cid / "index.npz"
    index.write_bytes(index.read_bytes()[:60])

    restarted = make_client()  # a fresh process reading the damaged file
    for response in (ask(restarted, cid, "car"), restarted.get(f"/api/collections/{cid}/documents"),
                     upload(restarted, cid, ("more.txt", FRUIT, "text/plain"))):
        assert response.status_code == 500 and error_of(response)["code"] == "storage_corruption", response.text
        no_internals(response, settings)
    assert restarted.get("/api/collections").json()["collections"] == []  # listing survives, skips the damaged one
    assert restarted.delete(f"/api/collections/{cid}").status_code == 204  # and it can be cleaned up


def test_persistence_failure_maps_to_500_storage_error(client, settings, monkeypatch):
    cid = create(client)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store_module.os, "replace", boom)
    r = upload(client, cid, ("cars.txt", CARS, "text/plain"))
    assert r.status_code == 500 and error_of(r)["code"] == "storage_error"
    no_internals(r, settings)
    monkeypatch.undo()
    assert client.get(f"/api/collections/{cid}/documents").json()["documents"] == []  # nothing half-indexed


def test_unexpected_errors_are_500_internal_error_and_logged_not_leaked(client, settings, monkeypatch, caplog):
    cid = create(client)

    def explode(*a, **k):
        raise RuntimeError("boom at /home/user/secret/path with sk-ant-api03-hunter2")

    monkeypatch.setattr(CollectionManager, "query", explode)
    with caplog.at_level("ERROR", logger="app.api"):
        r = ask(client, cid, "car")
    assert r.status_code == 500 and error_of(r) == {"code": "internal_error", "message": "An unexpected error occurred."}
    for secret in ("boom", "hunter2", "/home/user", "secret"):
        assert secret not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"
    assert any("unhandled error" in rec.getMessage() for rec in caplog.records)  # the detail went to the log


class FlakyEmbedder(FakeEmbedder):
    fail = False

    def embed(self, texts):
        if self.fail:
            raise ModelUnavailableError("could not download https://models.example/x.tar.gz to /home/user/.cache: timeout")
        return super().embed(texts)


def test_embedding_model_unavailable_maps_to_503_without_urls_or_paths(make_client, settings):
    flaky = FlakyEmbedder()
    client = make_client(embedder_=flaky)
    cid = create(client)
    upload(client, cid, ("cars.txt", CARS, "text/plain"))
    flaky.fail = True
    for r in (upload(client, cid, ("more.txt", FRUIT, "text/plain")), ask(client, cid, "car")):
        assert r.status_code == 503 and error_of(r)["code"] == "embedding_unavailable"
        assert "models.example" not in r.text and "/home/" not in r.text


def test_embedder_mismatch_maps_to_409(settings, make_client):
    first = make_client()
    cid = create(first)

    class Other(FakeEmbedder):
        name = "some-other-model"

    r = ask(make_client(embedder_=Other()), cid, "car")
    assert r.status_code == 409 and error_of(r)["code"] == "embedder_mismatch"
    assert "some-other-model" not in r.text


def test_app_starts_and_reports_not_ready_when_the_model_cannot_be_loaded(settings, tmp_path):
    lazy = LazyOnnxEmbedder(tmp_path / "cache", (tmp_path / "missing.tar.gz").as_uri(), "a" * 64)
    with TestClient(create_app(settings, embedder=lazy, generator=citing_generator()), raise_server_exceptions=False) as client:
        health = client.get("/api/health")  # lifespan started the background warm-up, which failed
        assert health.status_code == 503 and health.json()["ready"] is False and health.json()["status"] == "starting"
        assert health.json()["embedding"]["ready"] is False
        cid = create(client)  # creating collections needs no model
        r = upload(client, cid, ("cars.txt", CARS, "text/plain"))
        assert r.status_code == 503 and error_of(r)["code"] == "embedding_unavailable"
        assert str(tmp_path) not in r.text and "missing.tar.gz" not in r.text


# ---- hostile content -----------------------------------------------------------

def test_hostile_filenames_and_document_text_stay_inert_data(client):
    cid = create(client)
    name = "<img src=x onerror=alert(1)>'<svg onload=alert(2)>.txt"
    payload = b"<script>window.__pwned = 1</script> automobile engine <b>bold</b> &lt;"
    r = upload(client, cid, (name, payload, "text/html"))
    assert r.status_code == 201 and r.json()["indexed"][0]["filename"] == name  # returned verbatim, as JSON data
    assert r.headers["content-type"].startswith("application/json") and r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"]

    answer = ask(client, cid, "automobile engine")
    assert answer.headers["content-type"].startswith("application/json")
    body = answer.json()
    assert "<script>" in body["citations"][0]["snippet"] and body["citations"][0]["filename"] == name
    listing = client.get(f"/api/collections/{cid}/documents")
    assert listing.headers["content-type"].startswith("application/json") and name in listing.text


def test_html_documents_are_stripped_to_text_before_indexing(client):
    cid = create(client)
    upload(client, cid, ("page.html", b"<html><script>alert(1)</script><p>automobile engine</p><style>p{}</style></html>", "text/html"))
    snippet = ask(client, cid, "automobile engine").json()["citations"][0]["snippet"]
    assert "alert" not in snippet and "automobile engine" in snippet


def test_the_ui_is_served_with_a_strict_content_security_policy(client):
    page = client.get("/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert page.headers["x-frame-options"] == "DENY"
    for asset, kind in (("/app.js", "javascript"), ("/style.css", "css")):
        r = client.get(asset)
        assert r.status_code == 200 and kind in r.headers["content-type"]


def test_ui_source_never_turns_text_into_markup(client):
    html = client.get("/").text
    js = client.get("/app.js").text
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.I), "inline <script> would be blocked by the CSP"
    assert not re.search(r"\son\w+\s*=", html, re.I) and "javascript:" not in html.lower()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc", "DOMParser"):
        assert forbidden not in js, forbidden


# ---- deletion ------------------------------------------------------------------

def test_deleting_a_collection(client, settings):
    keep, drop = create(client, "keep"), create(client, "drop")
    upload(client, keep, ("fruit.txt", FRUIT, "text/plain"))
    upload(client, drop, ("cars.txt", CARS, "text/plain"))
    r = client.delete(f"/api/collections/{drop}")
    assert r.status_code == 204 and r.content == b""
    assert not (settings.data_dir / "collections" / drop).exists()
    assert [c["id"] for c in client.get("/api/collections").json()["collections"]] == [keep]
    for response in (ask(client, drop, "car"), client.get(f"/api/collections/{drop}/documents"), client.delete(f"/api/collections/{drop}"),
                     upload(client, drop, ("a.txt", CARS, "text/plain"))):
        assert response.status_code == 404 and error_of(response)["code"] == "collection_not_found"
    assert ask(client, keep, "apple orchard").json()["status"] == "answered"  # the other one is untouched


# ---- health --------------------------------------------------------------------

def test_health_reports_readiness_and_generation(client):
    r = client.get("/api/health")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok" and body["ready"] is True
    assert body["generation"] == {"configured": True, "model": "claude-opus-5-5", "detail": None}
    assert body["embedding"]["ready"] is True and body["embedding"]["model"] == "fake-concepts"
    assert body["limits"]["max_upload_bytes"] == 50_000 and body["limits"]["min_score"] == 0.15


def test_health_when_the_embedder_is_not_loaded_yet(make_client):
    class Loading(FakeEmbedder):
        loaded = False

    r = make_client(embedder_=Loading()).get("/api/health")
    assert r.status_code == 503 and r.json()["ready"] is False and r.json()["generation"]["configured"] is True


def test_health_never_exposes_the_api_key(settings, embedder):
    secret = "sk-ant-api03-DO-NOT-LEAK"
    configured = Settings.from_env({"RAG_DATA_DIR": str(settings.data_dir), "ANTHROPIC_API_KEY": secret})
    client = TestClient(create_app(configured, embedder=embedder), raise_server_exceptions=False)
    r = client.get("/api/health")
    assert r.json()["generation"]["configured"] is True and secret not in r.text and "sk-ant" not in r.text
    assert secret not in client.get("/openapi.json").text


def test_unavailable_generator_is_reported_by_health(make_client):
    r = make_client(UnavailableGenerator("no key")).get("/api/health")
    assert r.status_code == 200 and r.json()["generation"]["configured"] is False
