"""Historical Responses/image adapter, not wired into the current durable executor.

Do not enable without send fencing, local consent, network and budget parity.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from openai import OpenAI

from novel_harness.ai.base import (
    AIImageRequest,
    AITextRequest,
    ImageResult,
    ProviderConfigurationError,
    ProviderError,
    ProviderExecutionError,
    StructuredResult,
    TextResult,
)


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None,
        text_model: str,
        image_model: str,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.text_model = text_model
        self.image_model = image_model
        self._client = client

    def _client_or_raise(self):
        if not self.api_key:
            raise ProviderConfigurationError("请在后端环境中设置 OPENAI_API_KEY。")
        if self._client is None:
            self._client = OpenAI(api_key=self.api_key)
        return self._client

    @staticmethod
    def _input(request: AITextRequest) -> list[dict[str, str]]:
        user_content = request.user_prompt
        if request.context:
            user_content += "\n\n上下文：\n" + json.dumps(
                request.context, ensure_ascii=False, separators=(",", ":")
            )
        return [
            {"role": "developer", "content": request.developer_instruction},
            {"role": "user", "content": user_content},
        ]

    def generate_text(self, request: AITextRequest) -> TextResult:
        try:
            response = self._client_or_raise().responses.create(
                model=self.text_model,
                input=self._input(request),
            )
            return TextResult(text=response.output_text, provider=self.name, model=self.text_model)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderExecutionError(
                "OpenAI 文本生成失败；请检查网络、模型权限和配置。"
            ) from exc

    def generate_structured(
        self, request: AITextRequest, schema: dict[str, Any]
    ) -> StructuredResult:
        try:
            response = self._client_or_raise().responses.create(
                model=self.text_model,
                input=self._input(request),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "novel_harness_output",
                        "schema": schema,
                        "strict": True,
                    }
                },
            )
            return StructuredResult(
                data=json.loads(response.output_text),
                provider=self.name,
                model=self.text_model,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderExecutionError("OpenAI 结构化生成失败；请检查输出和模型配置。") from exc

    def generate_image(self, request: AIImageRequest) -> ImageResult:
        try:
            response = self._client_or_raise().images.generate(
                model=self.image_model,
                prompt=request.prompt,
                size=request.size,
                response_format="b64_json",
            )
            return ImageResult(
                data=base64.b64decode(response.data[0].b64_json),
                provider=self.name,
                model=self.image_model,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderExecutionError(
                "OpenAI 图像生成失败；请检查网络、模型权限和配置。"
            ) from exc
