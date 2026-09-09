import asyncio
import json


def _run_import_limit_asgi(app, messages, *, content_length=b"1"):
    from novel_harness.api.request_limits import RequestLimitMiddleware

    sent = []

    async def run():
        pending = list(messages)

        async def receive():
            if pending:
                return pending.pop(0)
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/imports",
            "headers": [(b"content-length", content_length)],
        }
        await RequestLimitMiddleware(app)(scope, receive, send)

    asyncio.run(run())
    return sent


def _response_starts(messages):
    return [message for message in messages if message["type"] == "http.response.start"]


def test_import_stream_overflow_sends_exactly_one_413_start(monkeypatch):
    from novel_harness.api import request_limits

    monkeypatch.setattr(request_limits, "IMPORT_BODY_BYTES", 5)

    async def consume_then_respond(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = _run_import_limit_asgi(
        consume_then_respond,
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ],
    )

    starts = _response_starts(sent)
    assert len(starts) == 1
    assert starts[0]["status"] == 413
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert json.loads(body)["detail"]["code"] == "IMPORT_UPLOAD_TOO_LARGE"


def test_import_stream_early_response_cannot_cause_double_start(monkeypatch):
    from novel_harness.api import request_limits

    monkeypatch.setattr(request_limits, "IMPORT_BODY_BYTES", 5)

    async def respond_before_consuming(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        await receive()
        await send({"type": "http.response.body", "body": b"ok"})

    sent = _run_import_limit_asgi(
        respond_before_consuming,
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ],
    )

    starts = _response_starts(sent)
    assert len(starts) == 1
    assert starts[0]["status"] == 200


def test_import_stream_overflow_disconnect_is_sticky_after_early_start(monkeypatch):
    from novel_harness.api import request_limits
    from novel_harness.api.request_limits import RequestLimitMiddleware

    monkeypatch.setattr(request_limits, "IMPORT_BODY_BYTES", 5)
    sent = []

    async def early_reader(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        assert (await receive())["type"] == "http.request"
        assert (await receive())["type"] == "http.disconnect"
        assert (await asyncio.wait_for(receive(), timeout=0.1))["type"] == "http.disconnect"
        await send({"type": "http.response.body", "body": b"done"})

    async def run():
        pending = [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": True},
        ]
        never = asyncio.Event()

        async def receive():
            if pending:
                return pending.pop(0)
            await never.wait()

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/imports",
            "headers": [(b"content-length", b"1")],
        }
        await RequestLimitMiddleware(early_reader)(scope, receive, send)

    asyncio.run(run())
    assert len(_response_starts(sent)) == 1


def test_import_stream_immediate_response_never_waits_for_unread_body(monkeypatch):
    from novel_harness.api import request_limits
    from novel_harness.api.request_limits import RequestLimitMiddleware

    monkeypatch.setattr(request_limits, "IMPORT_BODY_BYTES", 5)
    sent = []

    async def immediate_forbidden(scope, receive, send):
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"forbidden"})

    async def run():
        never = asyncio.Event()

        async def receive():
            await never.wait()

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/imports",
            "headers": [(b"content-length", b"1")],
        }
        await asyncio.wait_for(
            RequestLimitMiddleware(immediate_forbidden)(scope, receive, send),
            timeout=0.2,
        )

    asyncio.run(run())
    starts = _response_starts(sent)
    assert len(starts) == 1
    assert starts[0]["status"] == 403


def test_import_stream_forwards_large_early_response_without_buffering(monkeypatch):
    from novel_harness.api import request_limits
    from novel_harness.api.request_limits import RequestLimitMiddleware

    monkeypatch.setattr(request_limits, "IMPORT_BODY_BYTES", 5)
    sent = []

    async def streaming(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        assert len(sent) == 1
        for index in range(1000):
            await send(
                {
                    "type": "http.response.body",
                    "body": b"x",
                    "more_body": index < 999,
                }
            )
            assert len(sent) == index + 2

    async def run():
        async def receive():
            raise AssertionError("completed response must not wait for request body")

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/imports",
            "headers": [(b"content-length", b"1")],
        }
        await RequestLimitMiddleware(streaming)(scope, receive, send)

    asyncio.run(run())
    assert len(sent) == 1001


def test_import_stream_disconnect_does_not_break_asgi_protocol(monkeypatch):
    from novel_harness.api import request_limits

    monkeypatch.setattr(request_limits, "IMPORT_BODY_BYTES", 5)

    async def consume_disconnect(scope, receive, send):
        assert (await receive())["type"] == "http.request"
        assert (await receive())["type"] == "http.disconnect"

    sent = _run_import_limit_asgi(
        consume_disconnect,
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.disconnect"},
        ],
    )

    assert _response_starts(sent) == []


def test_declared_and_streamed_oversized_requests_are_rejected(client):
    response = client.post(
        "/api/v1/projects", content=b"{}", headers={"Content-Length": str(9 * 1024 * 1024)}
    )
    assert response.status_code == 413
    response = client.post("/api/v1/projects", content=b"x" * (8 * 1024 * 1024 + 1))
    assert response.status_code == 413


def test_only_exact_import_create_streams_past_generic_request_limit(client):
    large_text = b"x" * (8 * 1024 * 1024 + 1)
    imported = client.post(
        "/api/v1/imports",
        data={"source_kind": "folder", "display_name": "large", "paths": ["large.txt"]},
        files={"files": ("ignored-name.txt", large_text, "text/plain")},
    )
    assert imported.status_code == 201

    for method, path in (
        (client.post, "/api/v1/imports/other"),
        (client.patch, "/api/v1/imports"),
    ):
        response = method(path, content=large_text)
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "REQUEST_TOO_LARGE"


def test_import_stream_has_its_own_raw_body_hard_cap(client, monkeypatch):
    from novel_harness.api import request_limits

    monkeypatch.setattr(request_limits, "IMPORT_BODY_BYTES", 128)
    response = client.post(
        "/api/v1/imports",
        data={"source_kind": "folder", "display_name": "book", "paths": ["one.txt"]},
        files={"files": ("one.txt", b"x" * 256)},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "IMPORT_UPLOAD_TOO_LARGE"

    streamed = client.post(
        "/api/v1/imports",
        headers={"Content-Length": "1"},
        data={"source_kind": "folder", "display_name": "book", "paths": ["one.txt"]},
        files={"files": ("one.txt", b"x" * 256)},
    )
    assert streamed.status_code == 413
    assert streamed.json()["detail"]["code"] == "IMPORT_UPLOAD_TOO_LARGE"


def test_validation_does_not_echo_novel_or_secret_values(client):
    response = client.post("/api/v1/projects", json={"title": "PRIVATE_SENTINEL" * 1000})
    assert response.status_code == 422
    assert "PRIVATE_SENTINEL" not in response.text


def test_unexpected_error_has_safe_diagnostic_id_without_sensitive_logs(client, caplog):
    import logging

    @client.app.get("/test-diagnostic")
    def fail():
        raise RuntimeError("PRIVATE_SECRET_IN_EXCEPTION")

    client.app.router.routes.insert(0, client.app.router.routes.pop())
    with caplog.at_level(logging.WARNING):
        response = client.get("/test-diagnostic")
    assert response.status_code == 500
    diagnostic = response.json()["detail"]["diagnostic_id"]
    assert len(diagnostic) == 32
    assert diagnostic in caplog.text
    assert "PRIVATE_SECRET_IN_EXCEPTION" not in caplog.text + response.text
