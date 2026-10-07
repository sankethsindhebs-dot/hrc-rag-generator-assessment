"""Calibrate the retrieval-confidence threshold against the real embedding model.

Ingests the two sample collections, runs every question in samples/calibration.json, and
prints the distribution of top-1 cosine scores per question category plus how many
questions of each category would pass the gate at each candidate threshold.

Usage:  python scripts/calibrate_threshold.py [--top-k 4] [--verbose]
Needs the embedding model (downloaded and checksum-verified on first use).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.rag.collections import CollectionManager  # noqa: E402
from app.rag.embeddings import OnnxMiniLMEmbedder, ensure_model  # noqa: E402

SAMPLES = ROOT / "samples"
CATEGORIES = ["answerable", "paraphrase", "exact_term", "related_unanswered", "unrelated"]
SWEEP = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--verbose", action="store_true", help="print every question")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings.from_env({"RAG_DATA_DIR": tmp})
        embedder = OnnxMiniLMEmbedder(ensure_model(settings.model_cache_dir, settings.model_url, settings.model_sha256))
        manager = CollectionManager(settings, embedder)
        ids = {}
        for folder in sorted(p for p in SAMPLES.iterdir() if p.is_dir()):
            ids[folder.name] = manager.create_collection(folder.name).id
            for doc in sorted(folder.iterdir()):
                manager.add_document(ids[folder.name], doc.name, doc.read_bytes())
            print(f"ingested {folder.name}: {manager.get_collection(ids[folder.name]).chunk_count} chunks")

        questions = json.loads((SAMPLES / "calibration.json").read_text("utf-8"))["questions"]
        rows = []
        for q in questions:
            targets = list(ids) if q["collection"] == "*" else [q["collection"]]
            for target in targets:
                hits = manager.query(ids[target], q["question"], top_k=args.top_k)
                phrase = q.get("expected_phrase")
                found_rank = next((i + 1 for i, h in enumerate(hits) if phrase and phrase.lower() in h.chunk.text.lower()), None)
                rows.append({
                    "category": q["category"], "collection": target, "question": q["question"],
                    "top1": hits[0].score, "top1_file": hits[0].chunk.filename,
                    "phrase_rank": found_rank, "has_phrase": phrase is not None,
                    "evidence_scores": [round(h.score, 3) for h in hits],
                })

    if args.verbose:
        for r in sorted(rows, key=lambda r: (CATEGORIES.index(r["category"]), -r["top1"])):
            rank = f"rank{r['phrase_rank']}" if r["has_phrase"] and r["phrase_rank"] else ("MISSING" if r["has_phrase"] else "-")
            print(f"{r['category']:<19}{r['top1']:.3f}  {rank:<8} {r['collection'][:9]:<9} {r['question'][:60]}")

    print("\nTop-1 cosine score by category")
    print(f"{'category':<20}{'n':>3}{'min':>8}{'p25':>8}{'median':>8}{'p75':>8}{'max':>8}")
    for cat in CATEGORIES:
        s = sorted(r["top1"] for r in rows if r["category"] == cat)
        q = statistics.quantiles(s, n=4, method="inclusive")
        print(f"{cat:<20}{len(s):>3}{s[0]:>8.3f}{q[0]:>8.3f}{statistics.median(s):>8.3f}{q[2]:>8.3f}{s[-1]:>8.3f}")

    print("\nQuestions that PASS the gate (top-1 >= threshold), by category")
    print(f"{'threshold':<10}" + "".join(f"{c[:10]:>12}" for c in CATEGORIES))
    for t in SWEEP:
        cells = []
        for cat in CATEGORIES:
            sel = [r for r in rows if r["category"] == cat]
            cells.append(f"{sum(r['top1'] >= t for r in sel)}/{len(sel)}")
        print(f"{t:<10.2f}" + "".join(f"{c:>12}" for c in cells))

    answerable = [r for r in rows if r["has_phrase"]]
    hit = sum(r["phrase_rank"] is not None for r in answerable)
    print(f"\nEvidence recall: answer-bearing text was within the top-{args.top_k} for {hit}/{len(answerable)} answerable questions")
    worst = min(answerable, key=lambda r: r["top1"])
    best_unrelated = max((r for r in rows if r["category"] == "unrelated"), key=lambda r: r["top1"])
    print(f"Lowest answerable-type top-1:  {worst['top1']:.3f}  ({worst['question']!r})")
    print(f"Highest unrelated top-1:       {best_unrelated['top1']:.3f}  ({best_unrelated['question']!r} vs {best_unrelated['collection']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
