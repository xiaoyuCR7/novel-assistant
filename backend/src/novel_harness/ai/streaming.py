"""Bounded SSE/NDJSON text decoding; a partial preview is never a successful result."""

import codecs
import json
from time import monotonic

from novel_harness.ai.base import (
    MAX_RESPONSE_BYTES,
    ProviderExecutionError,
    incomplete_output,
    require_identity_encoding,
)


def read_text_stream(response, request, observer, deadline, *, local=False):
    require_identity_encoding(response)
    decoder, buffer, event_lines = codecs.getincrementaldecoder("utf-8")(), "", []
    fragments, total, metadata, complete = [], 0, {}, False
    chars, stream_ended = 0, False

    def consume(encoded):
        nonlocal complete, metadata, chars, stream_ended
        if not encoded:
            return
        if stream_ended:
            raise ValueError("Data after stream end")
        if encoded == "[DONE]":
            stream_ended = True
            return
        frame = json.loads(encoded)
        if not isinstance(frame, dict):
            raise ValueError("Invalid stream frame")
        if frame.get("error"):
            raise ValueError("Provider stream error")
        if local:
            if complete:
                raise ValueError("Data after terminal message")
            if not isinstance(frame.get("message", {}), dict):
                raise ValueError("Invalid message")
            delta = frame.get("message", {}).get("content", "")
            if frame.get("done"):
                if frame.get("done_reason") not in {None, "stop"}:
                    raise ValueError("Incomplete provider response")
                complete = True
                metadata = frame
        else:
            if frame.get("usage"):
                metadata["usage"] = frame["usage"]
            choices = frame.get("choices", [])
            if complete and choices:
                raise ValueError("Data after terminal choice")
            choice = choices[0] if choices else {}
            if not isinstance(choice, dict) or not isinstance(choice.get("delta", {}), dict):
                raise ValueError("Invalid choice")
            delta = choice.get("delta", {}).get("content") or ""
            if choice.get("finish_reason"):
                if choice["finish_reason"] in {"length", "content_filter"}:
                    raise incomplete_output(choice["finish_reason"])
                if choice["finish_reason"] != "stop":
                    raise ValueError("Incomplete provider response")
                complete = True
        if not isinstance(delta, str):
            raise ValueError("Invalid delta")
        chars += len(delta)
        if chars > request.output_token_budget * 8:
            raise ProviderExecutionError(
                "流式输出超过上限。", outcome="known", code="OUTPUT_LIMIT_EXCEEDED"
            )
        if delta:
            fragments.append(delta)
            if observer:
                observer.preview(delta)

    def line(value):
        value = value.rstrip("\r")
        if local:
            if value:
                consume(value)
        elif not value:
            consume("\n".join(event_lines))
            event_lines.clear()
        elif value.startswith("data:"):
            event_lines.append(value[5:].lstrip())

    for chunk in response.iter_bytes():
        if monotonic() > deadline:
            raise ProviderExecutionError(
                "请求总期限已到；不自动重发。", code="PROVIDER_RESULT_UNKNOWN"
            )
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ValueError("Stream size limit")
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            value, buffer = buffer.split("\n", 1)
            line(value)
    buffer += decoder.decode(b"", final=True)
    if buffer:
        line(buffer)
    if event_lines:
        consume("\n".join(event_lines))
    if not complete:
        raise ProviderExecutionError(
            "流式响应未完整结束；请明确处理，不自动重发。", code="PROVIDER_RESULT_UNKNOWN"
        )
    content = "".join(fragments)
    if local:
        return {**metadata, "message": {"content": content}, "done": True}
    return {**metadata, "choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
