"""Requirement R4: the same code serves unrelated document sets with no changes, and sets are isolated."""
from fastapi.testclient import TestClient

from rag.api import create_app
from rag.config import Settings
from tests.conftest import fixture_bytes
from tests.fakes import EvidenceLLM, HashEmbedder
from tests.fixtures.questions import POLICY, SOURDOUGH

SETTINGS = Settings(chunk_size=300, chunk_overlap=40, top_k=3, min_score=0.15)
BREAD_Q = "How often should I feed my sourdough starter?"
POLICY_Q = "How quickly must a security incident be reported?"


def new_collection(client, name):
    r = client.post("/collections", files=[("files", (name, fixture_bytes(name)[1], "text/plain"))])
    assert r.status_code == 201
    return r.json()["collection_id"]


def ask(client, cid, q):
    r = client.post(f"/collections/{cid}/ask", json={"question": q})
    assert r.status_code == 200
    return r.json()


def test_two_unrelated_document_sets_in_one_running_server():
    llm = EvidenceLLM()
    client = TestClient(create_app(embedder=HashEmbedder(), llm=llm, settings=SETTINGS))
    bread, policy = new_collection(client, SOURDOUGH), new_collection(client, POLICY)
    assert bread != policy

    # Each set answers its own questions, citing only its own document.
    a = ask(client, bread, BREAD_Q)
    b = ask(client, policy, POLICY_Q)
    assert a["sufficient"] and {s["doc"] for s in a["sources"]} == {SOURDOUGH}
    assert b["sufficient"] and {s["doc"] for s in b["sources"]} == {POLICY}

    # Cross-asking is insufficient, and never leaks the other set's content.
    for cid, q in [(bread, POLICY_Q), (policy, BREAD_Q)]:
        out = ask(client, cid, q)
        assert out["sufficient"] is False and out["sources"] == []


def test_uploading_a_new_set_does_not_disturb_the_old_one():
    client = TestClient(create_app(embedder=HashEmbedder(), llm=EvidenceLLM(), settings=SETTINGS))
    first = new_collection(client, SOURDOUGH)
    before = ask(client, first, BREAD_Q)
    new_collection(client, POLICY)
    assert ask(client, first, BREAD_Q) == before
