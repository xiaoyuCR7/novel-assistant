"""Bound request bytes before JSON decoding, including missing/false Content-Length."""

from starlette.responses import JSONResponse

MAX_BODY_BYTES = 8 * 1024 * 1024
IMPORT_BODY_BYTES = 200 * 1024 * 1024 + 16 * 1024 * 1024


class _ImportUploadTooLarge(Exception):
    pass


class RequestLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in {'POST', 'PUT', 'PATCH'}:
            return await self.app(scope, receive, send)
        if scope['method'] == 'POST' and scope['path'] == '/api/v1/imports':
            return await self._stream_import(scope, receive, send)
        headers = dict(scope['headers'])
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = MAX_BODY_BYTES + 1
        chunks, total = [], 0
        if length <= MAX_BODY_BYTES:
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                total += len(chunk)
                if total > MAX_BODY_BYTES:
                    break
                chunks.append(chunk)
                if not message.get('more_body', False):
                    body = b''.join(chunks)
                    delivered = False
                    async def replay(body=body):
                        nonlocal delivered
                        if not delivered:
                            delivered = True
                            return {'type': 'http.request', 'body': body, 'more_body': False}
                        return await receive()
                    return await self.app(scope, replay, send)
        response = JSONResponse(status_code=413, content={'detail': {
            'code': 'REQUEST_TOO_LARGE', 'message': '请求超过8 MiB上限，请分章或分批处理。',
        }})
        await response(scope, receive, send)

    async def _stream_import(self, scope, receive, send):
        headers = dict(scope['headers'])
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = IMPORT_BODY_BYTES + 1
        if length > IMPORT_BODY_BYTES:
            return await self._reject_import(scope, receive, send)
        total = 0
        exceeded = False
        disconnected = False
        response_started = False
        response_complete = False

        async def receive_limited():
            nonlocal disconnected, exceeded, total
            if exceeded or disconnected:
                return {'type': 'http.disconnect'}
            message = await receive()
            if message['type'] == 'http.disconnect':
                disconnected = True
                return message
            if message['type'] == 'http.request':
                total += len(message.get('body', b''))
                if total > IMPORT_BODY_BYTES:
                    exceeded = True
                    if response_started:
                        return {'type': 'http.disconnect'}
                    raise _ImportUploadTooLarge
            return message

        async def send_wrapper(message):
            nonlocal response_complete, response_started
            if exceeded and not response_started:
                return
            if message['type'] == 'http.response.start':
                if response_started:
                    return
                response_started = True
            elif message['type'] == 'http.response.body' and not message.get(
                'more_body', False
            ):
                response_complete = True
            if disconnected and not response_started:
                return
            await send(message)

        try:
            result = await self.app(scope, receive_limited, send_wrapper)
        except _ImportUploadTooLarge:
            result = None
        if exceeded:
            if response_started:
                if not response_complete:
                    await send({'type': 'http.response.body', 'body': b''})
                return result
            return await self._reject_import(scope, receive, send)
        return result

    @staticmethod
    async def _reject_import(scope, receive, send):
        response = JSONResponse(status_code=413, content={'detail': {
            'code': 'IMPORT_UPLOAD_TOO_LARGE',
            'message': '导入上传超过原始请求上限。',
        }})
        await response(scope, receive, send)
