"""The request-size cap works on the byte stream itself."""

import asyncio

from app.api.middleware import BodyLimitMiddleware, BodyTooLarge


def run_through(limit, chunks, headers=()):
    consumed = 0
    queue = list(chunks)

    async def receive():
        nonlocal consumed
        consumed += 1
        body = queue.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    reads = 0

    async def inner(scope, receive, send):
        nonlocal reads
        while True:
            message = await receive()
            reads += 1
            if not message.get("more_body"):
                return
        await send({"type": "http.response.start", "status": 200})

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/collections/abc/documents", "headers": list(headers)}
    middleware = BodyLimitMiddleware(inner, upload_limit=limit)
    try:
        asyncio.run(middleware(scope, receive, send))
        error = None
    except BodyTooLarge as exc:
        error = exc
    return error, consumed, sent


def test_streaming_stops_reading_as_soon_as_the_cap_is_crossed():
    error, consumed, _ = run_through(5_000, [b"x" * 1_000] * 1_000)  # a 1 MB body against a 5 KB cap
    assert error is not None and error.status_code == 413
    assert consumed == 6  # five chunks fit; the sixth crosses the cap and is the last one read


def test_a_body_exactly_at_the_cap_is_accepted():
    error, consumed, _ = run_through(5_000, [b"x" * 1_000] * 5)
    assert error is None and consumed == 5


def test_declared_content_length_over_the_cap_is_rejected_without_reading_anything():
    error, consumed, sent = run_through(5_000, [b"x"], headers=[(b"content-length", b"999999")])
    assert error is None and consumed == 0
    assert sent[0]["status"] == 413


def test_a_lying_content_length_does_not_help():
    error, consumed, _ = run_through(5_000, [b"x" * 1_000] * 100, headers=[(b"content-length", b"10")])
    assert error is not None and consumed == 6


def test_an_upload_never_pulls_more_than_limit_plus_one_bytes_into_memory(settings, embedder):
    """The route must bound its per-file read itself, not rely on the body cap alone."""
    from fastapi import Response

    from app.api.routes import Services, upload_documents
    from app.main import create_app
    from tests.helpers import FakeGenerator

    app = create_app(settings, embedder=embedder, generator=FakeGenerator(None))
    services: Services = app.state.services
    cid = services.manager.create_collection("c").id

    reads = []

    class Upload:
        filename = "huge.txt"

        class file:  # noqa: N801 - mimics a SpooledTemporaryFile
            @staticmethod
            def read(n=-1):
                reads.append(n)
                return b"a" * (settings.max_upload_bytes + 1 if n < 0 or n > settings.max_upload_bytes else n)

    response = Response()
    result = upload_documents(cid, response, files=[Upload()], s=services)
    assert reads == [settings.max_upload_bytes + 1]  # one bounded read, never read()
    assert response.status_code == 413 and result.rejected[0].code == "document_too_large"
    assert embedder.calls == []  # an oversized file is never embedded
