"""Tiny CLI:  python -m rag serve   |   python -m rag ask FILE [FILE ...] -q "question" """
import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .embeddings import FastEmbedEmbedder
from .grounding import answer_question
from .llm import ClaudeLLM
from .pipeline import build_collection


def main(argv: list[str] | None = None, embedder=None, llm=None) -> int:
    parser = argparse.ArgumentParser(prog="rag")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the web app")
    serve.add_argument("--port", type=int, default=8000)
    ask = sub.add_parser("ask", help="ask one question about local files")
    ask.add_argument("files", nargs="+", type=Path)
    ask.add_argument("-q", "--question", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("rag.api:app", host="127.0.0.1", port=args.port)
        return 0

    settings = Settings.from_env()
    embedder = embedder or FastEmbedEmbedder(settings.embed_model)
    llm = llm or ClaudeLLM(settings.llm_model)
    collection = build_collection([(p.name, p.read_bytes()) for p in args.files], embedder, settings)
    print(json.dumps(answer_question(collection, args.question, embedder, llm, settings).to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
