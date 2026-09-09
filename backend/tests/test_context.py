from novel_harness.services.context import ContextAssembler, ContextFragment


def test_context_budget_preserves_hard_constraints_and_drops_low_priority_soft_fragments():
    assembler = ContextAssembler()
    hard = [
        ContextFragment(
            source_type="chapter_contract",
            source_id="chapter-1",
            reason="当前章节不可裁剪的创作契约",
            priority=100,
            content="视角：林渡。不得揭示寄信人身份。",
            hard=True,
        ),
        ContextFragment(
            source_type="project_core",
            source_id="project-1",
            reason="项目核心前提",
            priority=100,
            content="死者只能在雾潮期间寄信。",
            hard=True,
        ),
    ]
    soft = [
        ContextFragment(
            source_type="canon_fact",
            source_id="fact-1",
            reason="章节契约引用林渡",
            priority=80,
            content="林渡二十四岁，在旧邮局值夜班。",
        ),
        ContextFragment(
            source_type="old_chapter",
            source_id="chapter-old",
            reason="全文检索命中低相关意象",
            priority=10,
            content="很久以前的支线章节。" * 100,
        ),
    ]

    packet = assembler.pack(
        task="draft",
        hard_fragments=hard,
        soft_fragments=soft,
        token_budget=sum(f.estimated_tokens for f in hard + soft[:1]),
    )

    assert {fragment.source_type for fragment in packet.fragments} == {
        "chapter_contract",
        "project_core",
        "canon_fact",
    }
    assert all(fragment.estimated_tokens > 0 for fragment in packet.fragments)
    assert all(fragment.reason for fragment in packet.fragments)
    assert packet.dropped_source_ids == ["chapter-old"]
    assert packet.fragments[0].hard is True
    assert packet.total_estimated_tokens <= packet.token_budget


def test_hard_constraints_may_exceed_budget_but_are_never_removed():
    assembler = ContextAssembler()
    hard = [
        ContextFragment(
            source_type="chapter_contract",
            source_id="contract",
            reason="不可裁剪",
            priority=100,
            content="硬约束" * 200,
            hard=True,
        )
    ]

    packet = assembler.pack("review", hard, [], token_budget=10)

    assert [fragment.source_id for fragment in packet.fragments] == ["contract"]
    assert packet.over_budget is True
