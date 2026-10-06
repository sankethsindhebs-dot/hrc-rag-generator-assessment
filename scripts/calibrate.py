"""Print retrieval-score distributions on the test fixtures to pick RAG_MIN_SCORE.

Usage:  python scripts/calibrate.py
Needs the real embedding model (downloads on first run, so huggingface.co must be reachable).
Scores are model-specific; rerun this whenever RAG_EMBED_MODEL changes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.config import Settings  # noqa: E402
from rag.embeddings import FastEmbedEmbedder  # noqa: E402
from rag.pipeline import build_collection, retrieve  # noqa: E402
from tests.fixtures.questions import ANSWERABLE, UNANSWERABLE  # noqa: E402

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main() -> None:
    s = Settings.from_env()
    emb = FastEmbedEmbedder(s.embed_model)
    files = [(p.name, p.read_bytes()) for p in sorted(FIX.glob("*.txt"))]
    col = build_collection(files, emb, s)
    good = [(q, retrieve(col, q, emb, s).top_score) for q, _ in ANSWERABLE]
    bad = [(q, retrieve(col, q, emb, s).top_score) for q in UNANSWERABLE]
    print(f"model={s.embed_model} chunks={len(col.chunks)}")
    print("ANSWERABLE");   [print(f"  {sc:.3f}  {q}") for q, sc in good]
    print("UNANSWERABLE"); [print(f"  {sc:.3f}  {q}") for q, sc in bad]
    lo, hi = min(sc for _, sc in good), max(sc for _, sc in bad)
    print(f"\nmin answerable={lo:.3f}  max unanswerable={hi:.3f}")
    if lo > hi:
        print(f"Clean gap. Suggested RAG_MIN_SCORE ~ {(lo + hi) / 2:.2f} (current: {s.min_score})")
    else:
        print("Distributions overlap: no threshold separates them. Keep the gate low and rely on the LLM.")


if __name__ == "__main__":
    main()
