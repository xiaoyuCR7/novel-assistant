import pytest

from novel_harness.services.preparation_templates import select_template_questions


@pytest.mark.parametrize(
    "genre, setting_key",
    [
        ("东方玄幻", "world.power_levels"),
        ("仙侠修真", "world.power_levels"),
        ("西方奇幻", "world.magic_limits"),
        ("都市生活", "world.social_reality"),
        ("现实职场", "world.professional_rules"),
        ("现代言情", "relationship.romance_boundaries"),
        ("本格推理", "plot.truth_and_clue_chain"),
        ("硬科幻", "world.technology_limits"),
        ("历史架空", "world.historical_boundary"),
        ("传统武侠", "world.martial_limits"),
        ("游戏电竞", "world.game_rules"),
        ("末世求生", "world.disaster_rules"),
        ("恐怖灵异", "world.supernatural_rules"),
        ("青春校园", "world.school_timeline"),
        ("军事战争", "world.war_constraints"),
    ],
)
def test_common_genres_select_a_specific_critical_question(genre, setting_key):
    questions = select_template_questions(genre, "主角必须完成目标", answered_keys=set())

    assert setting_key in {item["setting_key"] for item in questions}
    assert len(questions) <= 5


def test_unknown_genre_uses_general_questions_and_is_deterministic():
    first = select_template_questions("自定义实验小说", "记忆会被城市改写", set())
    second = select_template_questions("自定义实验小说", "记忆会被城市改写", set())

    assert first == second
    assert first
    assert all(item["origin"] == "template" for item in first)
    assert all(item["source_ids"] == ["project:core"] for item in first)


def test_mixed_genres_dedupe_and_answered_settings_are_suppressed():
    questions = select_template_questions(
        "历史仙侠言情",
        "修士在古代王朝中相爱",
        answered_keys={"world.power_levels"},
    )
    fingerprints = [item["fingerprint"] for item in questions]

    assert len(questions) <= 5
    assert len(fingerprints) == len(set(fingerprints))
    assert "world.power_levels" not in {item["setting_key"] for item in questions}
    assert {"world.historical_boundary", "relationship.romance_boundaries"}.issubset(
        {item["setting_key"] for item in questions}
    )
