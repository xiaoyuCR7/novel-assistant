"""Deterministic high-impact preparation questions for common novel genres."""

import hashlib
import re


def _q(
    setting_key,
    category,
    question,
    rationale,
    impact_areas,
    *,
    answer_format="short_text",
    options=(),
    keywords=(),
):
    return {
        "setting_key": setting_key,
        "category": category,
        "question": question,
        "rationale": rationale,
        "impact_areas": list(impact_areas),
        "priority": "high",
        "answer_format": answer_format,
        "options": list(options),
        "keywords": tuple(keywords),
    }


GENERAL = [
    _q(
        "plot.protagonist_goal",
        "plot",
        "主角贯穿全书、不能轻易放弃的核心目标是什么？",
        "核心目标决定长篇每一卷的推进方向。",
        ("plot", "character"),
        keywords=("目标", "寻找", "复仇", "拯救", "完成"),
    ),
    _q(
        "world.non_negotiable_rules",
        "world_rules",
        "这个世界有哪些绝不能被剧情便利打破的硬规则？",
        "硬规则若中途改变，会破坏冲突和解法的可信度。",
        ("continuity", "world"),
        answer_format="ordered_list",
        keywords=("规则", "代价", "不能", "禁止"),
    ),
    _q(
        "plot.ending_direction",
        "plot",
        "故事最终大致走向什么结局，哪些结果必须保留？",
        "结局方向会反向约束伏笔和人物弧光。",
        ("plot", "foreshadowing"),
        keywords=("结局", "最终", "死亡", "团聚"),
    ),
    _q(
        "narrative.pov_and_tense",
        "narrative",
        "全书采用什么视角与时态，允许在哪些边界内切换？",
        "稳定的叙事规则可避免长篇声音和知识边界漂移。",
        ("voice", "continuity"),
    ),
    _q(
        "story.time_and_scope",
        "structure",
        "故事覆盖多长时间和多大地域，主要阶段如何划分？",
        "时间与空间尺度会影响成长速度、旅行和事件密度。",
        ("timeline", "world"),
        keywords=("年", "时代", "世界", "城市"),
    ),
]


CATALOG = {
    "cultivation": [
        _q(
            "world.power_levels",
            "power",
            "修炼境界依次有哪些？",
            "境界是战力、寿命和剧情门槛的共同标尺。",
            ("power", "continuity"),
            answer_format="ordered_list",
            keywords=("修士", "修炼", "境界"),
        ),
        _q(
            "world.breakthrough_rules",
            "power",
            "突破每个境界需要什么条件，又会付出什么代价？",
            "突破规则决定成长是否可信。",
            ("power", "plot"),
        ),
        _q(
            "world.power_ceiling",
            "power",
            "越级战斗允许到什么程度，最高战力由什么限制？",
            "战力边界能防止后期体系失控。",
            ("power", "conflict"),
        ),
    ],
    "fantasy": [
        _q(
            "world.magic_limits",
            "world_rules",
            "魔法来自哪里，施展有哪些限制和代价？",
            "魔法限制决定冲突能否被随意解决。",
            ("world", "conflict"),
        ),
        _q(
            "world.species_rules",
            "world_rules",
            "主要种族有哪些稳定差异与相处规则？",
            "种族规则会反复影响社会和人物关系。",
            ("world", "character"),
        ),
        _q(
            "world.travel_and_technology",
            "world_rules",
            "世界的交通、通信和技术水平是什么？",
            "基础设施决定信息与行动速度。",
            ("timeline", "world"),
        ),
    ],
    "urban": [
        _q(
            "world.social_reality",
            "world_rules",
            "故事遵循现实社会到什么程度，哪些地方允许戏剧化处理？",
            "现实尺度会影响职业、法律和资源逻辑。",
            ("world", "tone"),
        ),
        _q(
            "character.work_and_income",
            "character",
            "主要人物的职业、收入和日常时间约束是什么？",
            "稳定的生活条件可避免人物行动脱离现实。",
            ("character", "timeline"),
        ),
        _q(
            "world.city_scope",
            "world_rules",
            "主要城市和活动区域如何分布，往返需要多久？",
            "城市尺度影响相遇、追踪和事件节奏。",
            ("world", "timeline"),
        ),
    ],
    "workplace": [
        _q(
            "world.professional_rules",
            "world_rules",
            "核心行业有哪些必须遵守的专业流程和硬约束？",
            "专业流程错误会直接损害现实可信度。",
            ("world", "continuity"),
        ),
        _q(
            "world.organization_power",
            "organization",
            "组织中的汇报关系、决策权和晋升规则是什么？",
            "权力结构决定职场冲突是否成立。",
            ("plot", "character"),
        ),
        _q(
            "character.professional_history",
            "character",
            "主要人物具备哪些可验证的履历与能力边界？",
            "履历决定角色能合理完成什么。",
            ("character", "continuity"),
        ),
    ],
    "romance": [
        _q(
            "relationship.romance_boundaries",
            "relationship",
            "核心感情关系的阶段、双方底线与不可接受行为是什么？",
            "关系边界决定感情推进是否尊重人物。",
            ("relationship", "character"),
        ),
        _q(
            "relationship.core_obstacle",
            "relationship",
            "阻止双方建立稳定关系的核心障碍是什么？",
            "稳定障碍可避免依赖重复误会拖延剧情。",
            ("relationship", "plot"),
        ),
        _q(
            "relationship.ending",
            "relationship",
            "核心感情线希望走向什么结局？",
            "结局倾向会约束中途承诺和人物选择。",
            ("relationship", "plot"),
        ),
    ],
    "mystery": [
        _q(
            "plot.truth_and_clue_chain",
            "mystery",
            "核心真相是什么，读者能够接触到的关键线索链有哪些？",
            "先确定真相与线索可避免后期强行圆谜。",
            ("plot", "foreshadowing"),
            answer_format="long_text",
            keywords=("谜", "案件", "真相", "凶手"),
        ),
        _q(
            "plot.fair_play_boundary",
            "mystery",
            "哪些信息必须公平展示，哪些可以延后揭示？",
            "信息边界决定推理是否公平。",
            ("knowledge", "plot"),
        ),
        _q(
            "plot.suspect_motives",
            "mystery",
            "主要嫌疑人的真实动机与表面动机分别是什么？",
            "动机网络能减少工具人和伪线索漏洞。",
            ("character", "plot"),
        ),
    ],
    "science_fiction": [
        _q(
            "world.technology_limits",
            "world_rules",
            "核心科技能做什么、不能做什么，代价是什么？",
            "科技边界决定问题能否被随意解决。",
            ("world", "continuity"),
            keywords=("科技", "飞船", "人工智能", "星际"),
        ),
        _q(
            "world.communication_and_distance",
            "world_rules",
            "通信、航行与时间延迟遵循什么规则？",
            "距离规则决定行动与信息的因果顺序。",
            ("timeline", "world"),
        ),
        _q(
            "world.technology_social_effect",
            "world_rules",
            "核心科技已经怎样改变社会制度和普通生活？",
            "社会后果能避免只有道具、没有世界变化。",
            ("world", "plot"),
        ),
    ],
    "historical": [
        _q(
            "world.historical_boundary",
            "world_rules",
            "哪些史实必须遵守，架空从哪个分歧点开始？",
            "史实边界能防止真实历史与虚构规则互相覆盖。",
            ("world", "timeline"),
            keywords=("朝代", "历史", "王朝"),
        ),
        _q(
            "world.historical_institutions",
            "world_rules",
            "政权、官制、法律和社会等级采用什么规则？",
            "制度决定人物能采取的合法与非法行动。",
            ("world", "plot"),
        ),
        _q(
            "world.historical_technology",
            "world_rules",
            "生产、交通、通信和武器处于什么水平？",
            "技术水平会约束旅行、战争和生活细节。",
            ("world", "timeline"),
        ),
    ],
    "wuxia": [
        _q(
            "world.martial_limits",
            "power",
            "武学层级与人体承受上限是什么？",
            "武力上限决定对决和伤势是否可信。",
            ("power", "continuity"),
        ),
        _q(
            "world.jianghu_rules",
            "world_rules",
            "门派、江湖与朝廷之间遵循什么秩序？",
            "江湖规则决定势力冲突和角色代价。",
            ("organization", "plot"),
        ),
        _q(
            "world.injury_recovery",
            "power",
            "重伤、中毒和内力消耗如何恢复？",
            "恢复规则可防止伤势按剧情需要消失。",
            ("continuity", "timeline"),
        ),
    ],
    "game": [
        _q(
            "world.game_rules",
            "world_rules",
            "游戏的胜负条件、核心机制和不可绕过的规则是什么？",
            "稳定规则是竞技与成长可信度的基础。",
            ("world", "conflict"),
        ),
        _q(
            "world.game_progression",
            "power",
            "等级、装备、技能和资源如何成长？",
            "数值成长必须能跨章节保持一致。",
            ("power", "continuity"),
        ),
        _q(
            "world.esports_format",
            "world_rules",
            "赛事赛制、队伍结构和赛季时间如何安排？",
            "赛制决定比赛目标和时间线。",
            ("plot", "timeline"),
        ),
    ],
    "apocalypse": [
        _q(
            "world.disaster_rules",
            "world_rules",
            "灾变如何发生和扩散，哪些区域与阶段不同？",
            "灾变规则决定持续风险与世界变化。",
            ("world", "timeline"),
        ),
        _q(
            "world.survival_resources",
            "world_rules",
            "食物、能源、药品和安全区的稀缺程度是什么？",
            "资源约束决定人物选择的代价。",
            ("world", "conflict"),
        ),
        _q(
            "world.infection_or_ability",
            "power",
            "感染、变异或能力的触发和限制是什么？",
            "稳定机制可避免危机强度随意变化。",
            ("power", "continuity"),
        ),
    ],
    "horror": [
        _q(
            "world.supernatural_rules",
            "world_rules",
            "超自然现象的触发、限制和逃生条件是什么？",
            "恐怖规则越稳定，未知越有压迫感。",
            ("world", "conflict"),
        ),
        _q(
            "world.horror_cost",
            "world_rules",
            "接触真相或使用异常力量需要付出什么代价？",
            "代价能防止角色轻易绕过恐怖。",
            ("character", "plot"),
        ),
        _q(
            "narrative.horror_boundary",
            "narrative",
            "恐怖尺度、禁区和读者可知信息边界是什么？",
            "尺度与知情边界决定全书体验。",
            ("tone", "knowledge"),
        ),
    ],
    "school": [
        _q(
            "world.school_timeline",
            "timeline",
            "故事处于哪个年级和学期，关键考试、假期与活动在何时？",
            "校园时间表会持续约束事件顺序。",
            ("timeline", "plot"),
        ),
        _q(
            "world.school_rules",
            "world_rules",
            "学校有哪些会实际影响人物行动的制度？",
            "制度是校园冲突成立的现实边界。",
            ("world", "character"),
        ),
        _q(
            "character.family_constraints",
            "character",
            "主要人物的家庭背景如何限制其选择？",
            "家庭条件会影响资源、关系和成长。",
            ("character", "relationship"),
        ),
    ],
    "war": [
        _q(
            "world.war_constraints",
            "world_rules",
            "战争双方的目标、兵力、补给和失败条件是什么？",
            "战争约束决定战略是否可信。",
            ("world", "plot"),
        ),
        _q(
            "world.command_structure",
            "organization",
            "指挥体系、军衔权限与通信方式是什么？",
            "指挥边界决定命令如何传递和失效。",
            ("organization", "timeline"),
        ),
        _q(
            "world.front_and_technology",
            "world_rules",
            "战线地理与武器技术水平是什么？",
            "地理和技术决定战术选择。",
            ("world", "conflict"),
        ),
    ],
}


ALIASES = {
    "cultivation": ("玄幻", "仙侠", "修真", "修仙"),
    "fantasy": ("奇幻", "魔幻"),
    "urban": ("都市",),
    "workplace": ("现实", "职场", "行业"),
    "romance": ("言情", "爱情", "恋爱"),
    "mystery": ("悬疑", "推理", "侦探", "刑侦"),
    "science_fiction": ("科幻", "星际", "赛博朋克"),
    "historical": ("历史", "架空", "古代"),
    "wuxia": ("武侠", "江湖"),
    "game": ("游戏", "电竞", "网游"),
    "apocalypse": ("末世", "灾变", "废土"),
    "horror": ("恐怖", "灵异", "惊悚"),
    "school": ("青春", "校园"),
    "war": ("军事", "战争", "军旅"),
}


def _normalized(value):
    return re.sub(r"[\s\W_]+", "", value.lower(), flags=re.UNICODE)


def _families(genre):
    normalized = _normalized(genre)
    return [
        family
        for family, aliases in ALIASES.items()
        if any(_normalized(alias) in normalized for alias in aliases)
    ]


def _rank(items, premise):
    normalized = _normalized(premise)
    return sorted(
        enumerate(items),
        key=lambda pair: (
            -sum(_normalized(keyword) in normalized for keyword in pair[1]["keywords"]),
            pair[0],
        ),
    )


def _persisted(item):
    fingerprint = hashlib.sha256(f"{item['category']}:{item['setting_key']}".encode()).hexdigest()
    return {
        "id": f"template-{fingerprint[:16]}",
        "fingerprint": fingerprint,
        "round": 1,
        **{key: value for key, value in item.items() if key != "keywords"},
        "source_ids": ["project:core"],
        "origin": "template",
        "status": "open",
        "answer": None,
        "canon_fact_id": None,
        "updated_at": None,
    }


def select_template_questions(genre, premise, answered_keys):
    """Return at most five stable, deduplicated questions without any model call."""
    families = _families(genre)
    selected = []
    ranked_by_family = {
        family: [item for _, item in _rank(CATALOG[family], premise)] for family in families
    }
    for family in families:
        if ranked_by_family[family]:
            selected.append(ranked_by_family[family].pop(0))
    remaining_specific = [item for family in families for item in ranked_by_family[family]]
    specific_target = min(3, len(remaining_specific) + len(selected))
    selected.extend(remaining_specific[: max(0, specific_target - len(selected))])
    selected.extend(item for _, item in _rank(GENERAL, premise))

    result, seen = [], set(answered_keys)
    for item in selected:
        key = item["setting_key"]
        if key in seen:
            continue
        seen.add(key)
        result.append(_persisted(item))
        if len(result) == 5:
            break
    return result
