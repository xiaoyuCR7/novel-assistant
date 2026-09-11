"""Deterministic offline provider for tests and first-run exploration."""

from __future__ import annotations

import base64
from typing import Any

from novel_harness.ai.base import (
    AIImageRequest,
    AITextRequest,
    ImageResult,
    StructuredResult,
    TextResult,
)

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class DemoProvider:
    name = "demo"
    model = "deterministic-novel-demo-v1"

    def generate_text(self, request: AITextRequest) -> TextResult:
        purpose = request.context.get("contract", {}).get("purpose", "推进当前章节")
        if request.task in {"chat", "connection_test"}:
            text = (
                "这是离线演示回复，不是模型推理。你可以先明确本章目标，"
                "再让角色面对一个必须做出的选择。连接真实模型后，"
                "我会结合当前小说资料和对话继续讨论。"
            )
        elif request.task == "conversation_summary":
            import json

            data = json.loads(request.user_prompt)
            text = "离线演示记忆摘录（非语义总结）：" + data["transcript_part"][:
                min(120, request.output_token_budget // 4)
            ]
        elif request.task == "scene_description":
            text = (
                "雾潮退到第七根系缆柱时，码头才从水声里显出轮廓。"
                "湿冷的木板贴着鞋底吱响，远处每一次铁链碰撞，"
                "都像有人在黑暗中试着打开一扇看不见的门。"
            )
        elif request.task == "quality_rewrite":
            import json

            text = json.loads(request.user_prompt)["manuscript"]
        elif request.task == "rewrite":
            text = (
                "雾从钟楼背后落下来时，林渡正把最后一封信压进邮袋。"
                "信封没有地址，火漆却留着他的指纹。\n\n"
                "他没有立刻拆。守钟人隔着柜台擦拭一枚早已停摆的怀表，"
                "仿佛只要谁先开口，今晚就会多出一个无法收回的事实。"
                f"林渡记得自己的任务：{purpose}。于是他把信贴近灯火，"
                "看见纸背慢慢浮出一行尚未写下的日期。"
            )
        else:
            text = (
                "夜班铃响过三次，旧邮局的门才向里开了一条缝。"
                "林渡接过无主邮袋，没有问守钟人为何戴着黑手套。"
                f"今晚他必须完成一件事：{purpose}。"
            )
        return TextResult(text=text, provider=self.name, model=self.model)

    def generate_structured(
        self, request: AITextRequest, schema: dict[str, Any]
    ) -> StructuredResult:
        if request.task in {"quality_review", "quality_final_review"}:
            data = {
                "scores": {
                    key: 85
                    for key in ("readability", "engagement", "pacing", "clarity", "consistency")
                },
                "summary": "离线演示质量报告，用于验证流程，不代表真实文学评分。",
                "issues": [],
                "next_guidance": "承接本章结尾继续推进。",
                "preserves_story": True,
            }
        elif request.task == 'wiki_summary':
            import json
            source = json.loads(request.user_prompt)['sources'][0]
            data = {'claims':[{'text':'离线演示摘录：'+source['content'][:120],
                               'source_ids':[f"{source['type']}:{source['id']}"]}]}
        elif request.task == "import_memory":
            data = {"candidates": []}
        elif request.task == "import_summary_map":
            start = request.context["untrusted_source_start"]
            end = request.context["untrusted_source_end"]
            body = request.user_prompt.split(start, 1)[1].split(end, 1)[0]
            quote = body[: min(80, len(body))]
            data = {
                "recap": body[: min(1000, len(body))],
                "evidence": [{"quote": quote, "start": 0, "end": len(quote)}],
            }
        elif request.task == "import_summary_merge":
            import json

            start = request.context["untrusted_data_start"]
            end = request.context["untrusted_data_end"]
            payload = request.user_prompt.split(start, 1)[1].split(end, 1)[0]
            partials = json.loads(payload)
            data = {"recap": "\n".join(item["recap"] for item in partials)[:4000]}
        elif request.task == "chapter_summary":
            import json

            manuscript = request.user_prompt.strip()
            if request.context.get("summary_mode") == "merge":
                summaries = json.loads(manuscript)
                # This is deliberately extractive, not a literary-quality model.
                data = summaries[-1]
                return StructuredResult(data=data, provider=self.name, model=self.model)
            data = {
                "recap": manuscript[:100]
                if request.context.get("summary_mode")
                else manuscript[:800],
                "plot_changes": [],
                "character_states": [],
                "knowledge_boundaries": [],
                "world_changes": [],
                "open_threads": [],
                "end_state": manuscript[-60:]
                if request.context.get("summary_mode")
                else manuscript[-300:],
                "fact_candidates": [],
                "content_findings": [],
                "content_observations": [],
                "evidence": [
                    {"field": "recap", "index": 0, "quote": manuscript[:60]},
                    {"field": "end_state", "index": 0, "quote": manuscript[-60:]},
                ],
            }
        elif request.task in {"preparation_analysis", "preparation_followup"}:
            data = {
                "questions": [
                    {
                        "setting_key": "world.recurring_delivery_rules",
                        "category": "world_rules",
                        "question": "亡者信件出现、送达与失效分别遵循什么固定规则？",
                        "rationale": "这是会跨章节重复触发的核心机制，需要提前固定边界。",
                        "impact_areas": ["world", "continuity", "plot"],
                        "priority": "high",
                        "answer_format": "long_text",
                        "options": [],
                        "source_ids": ["project:core"],
                    }
                ]
            }
        elif request.task == "plan":
            data = {
                "scenes": [
                    {
                        "title": "无主邮袋",
                        "goal": "让主角接下不可撤销的投递任务",
                        "obstacle": "信件署名与主角身份冲突",
                        "turn": "纸背显出未来日期",
                        "state_change": "从拒绝介入到决定调查",
                    }
                ]
            }
        elif request.task in {"review", "continuity_review", "style_review", "final_review"}:
            data = {"issues": [], "summary": "演示模式未发现阻断性问题。"}
        elif request.task in {"resolve", "suggest"}:
            data = {
                "options": [
                    {"mode": "conservative", "benefit": "保持现有结构", "risk": "惊奇有限"},
                    {"mode": "balanced", "benefit": "加强因果", "risk": "需调整一处动机"},
                    {"mode": "radical", "benefit": "扩大转折", "risk": "影响后续伏笔"},
                ]
            }
        else:
            data = {"result": "demo"}
        return StructuredResult(data=data, provider=self.name, model=self.model)

    def generate_image(self, request: AIImageRequest) -> ImageResult:
        return ImageResult(
            data=_ONE_PIXEL_PNG,
            provider=self.name,
            model="deterministic-image-demo-v1",
        )
