import pytest
from fastapi.testclient import TestClient

from rag.api import create_app
from rag.config import Settings
from rag.embeddings import EmbeddingError
from rag.llm import ConfigError, LLMError
from tests.conftest import fixture_bytes
from tests.fakes import EvidenceLLM, HashEmbedder, ScriptedLLM
from tests.fixtures.questions import POLICY, SOURDOUGH
from tests.test_loaders import make_pdf

SETTINGS = Settings(chunk_size=300, chunk_overlap=40, top_k=3, min_score=0.15)


def make_client(llm=None, embedder=None):
    llm = llm or EvidenceLLM()
    app = create_app(embedder=embedder or HashEmbedder(), llm=llm, settings=SETTINGS)
    return TestClient(app), llm


def upload(client, *names, extra=()):
    files = [("files", (n, fixture_bytes(n)[1], "text/plain")) for n in names] + list(extra)
    return client.post("/collections", files=files)


def test_index_page_and_health():
    client, _ = make_client()
    page = client.get("/")
    assert page.status_code == 200 and 'type="file"' in page.text and "Insufficient context" in page.text
    assert client.get("/health").json()["status"] == "ok"


def test_upload_then_ask_returns_grounded_answer_with_sources():
    client, _ = make_client()
    r = upload(client, POLICY)
    assert r.status_code == 201
    body = r.json()
    assert body["documents"] == [POLICY] and body["n_chunks"] > 0
    out = client.post(f"/collections/{body['collection_id']}/ask",
                      json={"question": "How much is the monthly internet stipend?"}).json()
    assert out["sufficient"] is True and out["sources"][0]["doc"] == POLICY
    assert out["sources"][0]["snippet"] and out["sources"][0]["page"] == 1


def test_offtopic_question_is_insufficient_without_calling_llm():
    llm = ScriptedLLM("should not be used")
    client, _ = make_client(llm)
    cid = upload(client, POLICY).json()["collection_id"]
    out = client.post(f"/collections/{cid}/ask", json={"question": "Who won the football world cup?"}).json()
    assert out["sufficient"] is False and out["reason"] == "retrieval_gate" and out["sources"] == []
    assert llm.calls == []


def test_pdf_and_txt_upload_together():
    client, _ = make_client()
    pdf = ("files", ("report.pdf", make_pdf(["The Zephyr project launches in March."]), "application/pdf"))
    r = upload(client, POLICY, extra=[pdf])
    assert r.status_code == 201 and sorted(r.json()["documents"]) == [POLICY, "report.pdf"]


def test_unknown_collection_is_404():
    client, _ = make_client()
    assert client.post("/collections/nope/ask", json={"question": "hi"}).status_code == 404


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "x" * 1001}])
def test_invalid_question_is_422(payload):
    client, _ = make_client()
    cid = upload(client, POLICY).json()["collection_id"]
    assert client.post(f"/collections/{cid}/ask", json=payload).status_code == 422


def test_whitespace_question_is_422():
    client, _ = make_client()
    cid = upload(client, POLICY).json()["collection_id"]
    assert client.post(f"/collections/{cid}/ask", json={"question": "   "}).status_code == 422


def test_upload_errors():
    client, _ = make_client()
    assert client.post("/collections").status_code == 422                       # no files
    bad_type = upload(client, extra=[("files", ("a.docx", b"x", "application/octet-stream"))])
    assert bad_type.status_code == 400 and "unsupported" in bad_type.json()["detail"]
    corrupt = upload(client, extra=[("files", ("bad.pdf", b"nope", "application/pdf"))])
    assert corrupt.status_code == 400 and "bad.pdf" in corrupt.json()["detail"]
    empty = upload(client, extra=[("files", ("e.txt", b"  ", "text/plain"))])
    assert empty.status_code == 400


def test_oversized_upload_rejected():
    app = create_app(embedder=HashEmbedder(), llm=EvidenceLLM(), settings=Settings(max_file_mb=0))
    r = TestClient(app).post("/collections", files=[("files", ("a.txt", b"hello", "text/plain"))])
    assert r.status_code == 400 and "larger than" in r.json()["detail"]


def test_llm_not_configured_is_503_and_upload_still_works():
    class Unconfigured:
        def generate(self, *a):
            raise ConfigError("RAG_LLM_MODEL is not set")
    client, _ = make_client(Unconfigured())
    cid = upload(client, POLICY).json()["collection_id"]
    r = client.post(f"/collections/{cid}/ask", json={"question": "How much is the monthly internet stipend?"})
    assert r.status_code == 503 and "RAG_LLM_MODEL" in r.json()["detail"]


def test_llm_failure_is_502():
    class Broken:
        def generate(self, *a):
            raise LLMError("Claude API error (InternalServerError)")
    client, _ = make_client(Broken())
    cid = upload(client, POLICY).json()["collection_id"]
    r = client.post(f"/collections/{cid}/ask", json={"question": "How much is the monthly internet stipend?"})
    assert r.status_code == 502


def test_embedding_model_unavailable_is_503():
    class NoModel:
        def embed_documents(self, texts):
            raise EmbeddingError("could not load embedding model")
        embed_query = embed_documents
    client, _ = make_client(embedder=NoModel())
    r = upload(client, POLICY)
    assert r.status_code == 503 and "embedding model" in r.json()["detail"]
