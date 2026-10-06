import json

from rag.__main__ import main
from tests.fakes import EvidenceLLM, HashEmbedder
from tests.conftest import FIXTURES


def test_cli_ask_prints_grounded_json(capsys, monkeypatch):
    monkeypatch.setenv("RAG_MIN_SCORE", "0.15")
    rc = main(["ask", str(FIXTURES / "remote_policy.txt"), "-q", "How much is the monthly internet stipend?"],
              embedder=HashEmbedder(), llm=EvidenceLLM())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["sufficient"] is True and out["sources"][0]["doc"] == "remote_policy.txt"
