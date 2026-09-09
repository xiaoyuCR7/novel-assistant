import pytest
from pydantic import ValidationError

from novel_harness.ai.base import (
    AITextRequest,
    ContextBudgetError,
    ProviderExecutionError,
    check_input_budget,
)
from novel_harness.ai.demo import DemoProvider
from novel_harness.services import summary_generation as generation


def _content_check_result(source="林渡绕开钟楼。"):
    request = AITextRequest(task="chapter_summary", developer_instruction="", user_prompt=source)
    result = DemoProvider().generate_structured(request, {}).model_dump()
    result["data"].update(
        content_findings=[
            {
                "dimension": "theme_alignment",
                "severity": "warning",
                "message": "本段暂时离开章节目标。",
                "evidence_quote": "绕开钟楼",
                "suggestion": "补充与钟楼任务的因果联系。",
                "reference_ids": ["project:core"],
            }
        ],
        content_observations=[
            {
                "kind": "action",
                "entity_id": "entity:lin-du",
                "predicate": "location",
                "value": "钟楼外",
                "evidence_quote": "林渡绕开钟楼",
                "reference_ids": ["entity:lin-du"],
            }
        ],
    )
    return result


def test_content_check_evidence_is_source_bound_and_gets_absolute_offsets():
    source = "开头。林渡绕开钟楼。结尾。"
    result = generation.validate_result(
        _content_check_result(source),
        source,
        allowed_reference_ids={"project:core", "entity:lin-du"},
    )

    finding = result["data"]["content_findings"][0]
    observation = result["data"]["content_observations"][0]
    assert source[finding["start"] : finding["end"]] == finding["evidence_quote"]
    assert source[observation["start"] : observation["end"]] == observation["evidence_quote"]


@pytest.mark.parametrize("broken", ["quote", "reference", "severity"])
def test_content_check_rejects_unverifiable_or_model_severe_output(broken):
    source = "林渡绕开钟楼。"
    result = _content_check_result(source)
    if broken == "quote":
        result["data"]["content_findings"][0]["evidence_quote"] = "正文没有这句话"
    elif broken == "reference":
        result["data"]["content_findings"][0]["reference_ids"] = ["entity:unknown"]
    else:
        result["data"]["content_findings"][0]["severity"] = "severe"

    with pytest.raises((ProviderExecutionError, ValidationError)):
        generation.validate_result(
            result,
            source,
            allowed_reference_ids={"project:core", "entity:lin-du"},
        )


def test_map_content_check_has_stricter_finding_and_observation_caps():
    source = "林渡绕开钟楼。"
    result = _content_check_result(source)
    result["data"]["content_findings"] *= 13
    result["data"]["content_observations"] *= 31

    with pytest.raises(ProviderExecutionError) as error:
        generation.validate_result(
            result,
            source,
            allowed_reference_ids={"project:core", "entity:lin-du"},
            max_content_findings=12,
            max_content_observations=30,
        )
    assert error.value.code == "CONTENT_CHECK_LIMIT_EXCEEDED"


def test_generated_summary_rejects_unknown_top_level_fields():
    source = "林渡绕开钟楼。"
    result = _content_check_result(source)
    result["data"]["untrusted_extension"] = "must not be ignored"

    with pytest.raises(ValidationError):
        generation.validate_result(
            result,
            source,
            allowed_reference_ids={"project:core", "entity:lin-du"},
        )


def test_reduce_cannot_drop_verified_findings_from_earlier_chunks():
    class Runner:
        def run(
            self,
            key,
            inputs,
            invoke,
            validate,
            *,
            allow_known_failure=False,
            nonterminal_known_codes=None,
        ):
            return validate(invoke(None))

    provider = DemoProvider()

    def invoke(request, observer, schema):
        result = provider.generate_structured(request, schema).model_dump()
        if request.context["summary_mode"] == "extract":
            result["data"]["content_findings"] = [
                {
                    "dimension": "theme_alignment",
                    "severity": "warning",
                    "message": f"{marker}发现需要保留。",
                    "evidence_quote": marker,
                    "suggestion": f"核对{marker}与主题的联系。",
                    "reference_ids": [],
                }
                for marker in ("首段标记", "尾段标记")
                if marker in request.user_prompt
            ]
        return result

    source = "首段标记。" + "林渡打开邮袋，看见一封信。\n" * 800 + "尾段标记。"
    result = generation.generate(source, 4096, Runner(), invoke)

    assert [item["evidence_quote"] for item in result["data"]["content_findings"]] == [
        "首段标记",
        "尾段标记",
    ]
    assert result["data"]["content_findings"][0]["start"] == 0
    assert result["data"]["content_findings"][1]["end"] == len(source) - 1


def test_unbounded_audit_union_preserves_observations_after_display_limit():
    partials = [
        {"content_observations": [{"predicate": f"p-{index}"} for index in range(40)]},
        {"content_observations": [{"predicate": f"p-{index}"} for index in range(40, 80)]},
    ]

    audit = generation._content_check_audit(partials)
    display = generation._verified_union(partials, "content_observations", limit=60)

    assert len(audit["observations"]) == 80
    assert audit["observations"][-1]["predicate"] == "p-79"
    assert len(display) == 60


def test_long_input_has_bounded_budgeted_stages_and_reuses_completed_chunks():
    cache, calls = {}, []
    broken = True

    class Runner:
        def run(
            self, key, inputs, invoke, validate, *, allow_known_failure=False,
            nonterminal_known_codes=None,
        ):
            nonlocal broken
            if key in cache:
                return cache[key]
            request = AITextRequest.model_validate(inputs["request"])
            assert check_input_budget(request, inputs["schema"]) <= request.token_budget
            calls.append(key)
            if key == "chapter_summary.chunk.1" and broken:
                broken = False
                raise RuntimeError("interrupted")
            cache[key] = validate(invoke(None))
            return cache[key]

    provider = DemoProvider()
    def invoke(request, observer, schema):
        return provider.generate_structured(request, schema).model_dump()
    source = "林渡打开邮袋，看见一封信。\n" * 800
    with pytest.raises(RuntimeError, match="interrupted"):
        generation.generate(source, 4096, Runner(), invoke)
    result = generation.generate(source, 4096, Runner(), invoke)
    assert result["data"]["evidence"]
    assert calls.count("chapter_summary.chunk.0") == 1
    assert len(calls) < 30


def test_planned_summary_calls_over_task_cap_fail_before_provider_dispatch(monkeypatch):
    calls = []

    class Runner:
        def run(self, *args, **kwargs):
            calls.append(args[0])
            raise AssertionError("provider stage must not start")

    monkeypatch.setattr(generation, "MAX_PROVIDER_CALLS", 2, raising=False)
    source = "林渡打开邮袋，看见一封信。\n" * 800

    with pytest.raises(ContextBudgetError, match="调用"):
        generation.generate(source, 4096, Runner(), lambda *_: None)
    assert calls == []


@pytest.mark.parametrize("bad", ["quote", "claim", "size"])
def test_invalid_summary_never_becomes_success_checkpoint(bad):
    request = AITextRequest(
        task="chapter_summary", developer_instruction="", user_prompt="林渡打开邮袋。"
    )
    result = DemoProvider().generate_structured(request, {}).model_dump()
    if bad == "quote":
        result["data"]["evidence"][0]["quote"] = "正文没有这句话"
    elif bad == "claim":
        result["data"]["character_states"] = ["林渡已经死亡"]
    else:
        result["data"]["recap"] = "a" * 8001
    with pytest.raises((ProviderExecutionError, ValueError)):
        generation.validate_result(result, request.user_prompt)


def test_successful_intermediate_must_fit_next_merge():
    saved = []

    class Runner:
        def run(
            self, key, inputs, invoke, validate, *, allow_known_failure=False,
            nonterminal_known_codes=None,
        ):
            result = validate(invoke(None))
            saved.append(key)
            return result

    def invoke(request, observer, schema):
        result = DemoProvider().generate_structured(request, schema).model_dump()
        result["data"]["recap"] = request.user_prompt[:7000]
        return result

    with pytest.raises(ProviderExecutionError, match="合并预算"):
        generation.generate("林渡拿到了信。" * 7000, 12000, Runner(), invoke)
    assert saved == []


def test_known_semantic_evidence_failure_gets_one_strict_repair_attempt():
    calls = []

    class Runner:
        def run(
            self, key, inputs, invoke, validate, *, allow_known_failure=False,
            nonterminal_known_codes=None,
        ):
            calls.append(
                (key, nonterminal_known_codes, inputs["request"]["developer_instruction"])
            )
            return validate(invoke(None))

    attempts = 0

    def invoke(request, observer, schema):
        nonlocal attempts
        attempts += 1
        result = DemoProvider().generate_structured(request, schema).model_dump()
        if attempts == 1:
            result["data"]["plot_changes"] = ["漏掉证据的情节变化"]
        return result

    result = generation.generate("林渡在北塔交出钥匙。", 4096, Runner(), invoke)

    assert result["data"]["evidence"]
    assert attempts == 2
    assert calls[0][0] == "chapter_summary"
    assert "MISSING_SUMMARY_EVIDENCE" in calls[0][1]
    assert calls[1][0] == "chapter_summary.evidence_repair"
    assert calls[1][1] is None
    assert "逐项自检" in calls[1][2]


def test_merge_preserves_supplied_offset_for_repeated_quotes():
    source = "他拿起信。后来，他拿起信。"
    quote = "拿起信"
    request = AITextRequest(task="chapter_summary", developer_instruction="", user_prompt=quote)
    value = DemoProvider().generate_structured(request, {}).model_dump()
    supplied = [
        {
            "field": "recap",
            "quote": quote,
            "start": source.rfind(quote),
            "end": source.rfind(quote) + len(quote),
        }
    ]
    result = generation.validate_result(value, source, supplied=supplied)
    assert all(item["start"] == source.rfind(quote) for item in result["data"]["evidence"])
