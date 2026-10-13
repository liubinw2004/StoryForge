"""各智能体提示词模板，统一对 JSON 输出做**强约束**。

针对风险 R1（LLM 输出 JSON 不稳定），所有需要结构化输出的提示词都：
① 明确要求「只输出 JSON，不要任何解释」；② 给出字段 schema；
③ 配合 :func:`app.services.json_parser.parse_json_lenient` 与自动重试。
"""

from __future__ import annotations

import json

__all__ = [
    "STYLE_LABELS",
    "style_label",
    "script_system_prompt",
    "script_user_prompt",
    "story_system_prompt",
    "story_user_prompt",
    "script_user_prompt_from_story",
    "json_retry_prompt",
    "critic_script_system_prompt",
    "critic_script_user_prompt",
    "critic_video_system_prompt",
    "critic_video_user_prompt",
    "VIDEO_STYLE_PREFIX",
    "PROMPT_MAX_CHARS",
    "LOCK_CARD_MAX_CHARS",
    "build_video_prompt",
]

STYLE_LABELS: dict[str, str] = {
    "realistic": "写实",
    "anime": "动漫",
    "animation": "动画",
}


def style_label(style: str) -> str:
    """把风格代号转为中文标签。"""
    return STYLE_LABELS.get(style, style or "写实")


_SCRIPT_SCHEMA = """{
  "title": "短剧标题（中文）",
  "style": "realistic | anime | animation",
  "characters": [
    {"name": "角色名", "desc": "外貌与服装等固定特征描述（中文）",
     "lock_card": "English appearance lock card (hair / face / age / clothing / body — time-invariant)"}
  ],
  "scene": "场景总述（时间、地点、光线氛围）（中文）",
  "scenes": [
    {"index": 0, "name": "场景名（如 咖啡馆-白天）", "desc": "场景中文描述",
     "lock_card": "English scene lock card (location / time of day / lighting / color tone / key props)"}
  ],
  "positions": ["1. 角色站位/调度说明", "2. ..."],
  "shots": [
    {
      "shot_id": "shot_001",
      "scene_index": 0,
      "dialogue": "本镜头对白（可含角色名前缀）",
      "action": "本镜头画面动作描述（中文）",
      "camera": "运镜说明，如 中景，缓慢推进",
      "duration": 4,
      "prompt": "English prompt for the video model, cinematic, consistent style"
    }
  ]
}"""

#: 拼接视频提示词时置于最前的**风格前缀**（统一打光与调色，强化跨镜头一致性）。
VIDEO_STYLE_PREFIX = "cinematic, consistent lighting and color grading"
#: 单条视频提示词的字符预算上限（超出时按「先丢尾部角色卡、再截 SCENE、SHOT 不截断」裁剪）。
PROMPT_MAX_CHARS = 1000
#: 单张锁定卡（角色/场景）的最大字符数。
LOCK_CARD_MAX_CHARS = 220


def _truncate_card(text: str, limit: int = LOCK_CARD_MAX_CHARS) -> str:
    """把单张锁定卡裁剪到 ``limit`` 字符以内（去首尾空白；超长取前缀）。"""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip()


def _normalize_card(item) -> tuple[str, str]:
    """把一张角色卡归一化为 ``(name, card)``。

    支持两种入参形态：
    - ``(name, card)`` 元组/二元列表 → 拆出名字；
    - 纯字符串 → 名字为空。
    """
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return str(item[0] or "").strip(), str(item[1] or "").strip()
    return "", str(item or "").strip()


def build_video_prompt(
    shot_prompt: str,
    style: str,
    character_cards,
    scene_card: str,
    *,
    budget: int = PROMPT_MAX_CHARS,
) -> str:
    """把「风格前缀 + 场景锁定卡 + 角色锁定卡 + 镜头主体」拼成单行英文提示词。

    输出格式（单行纯文本）::

        "{VIDEO_STYLE_PREFIX}, {style} style | SCENE: {scene_card} | CHARACTERS: {n1}={c1}; {n2}={c2} | SHOT: {shot_prompt}"

    - 无角色卡时**省略** ``CHARACTERS`` 段；无场景卡时**省略** ``SCENE`` 段；
    - 每张卡先按 :data:`LOCK_CARD_MAX_CHARS` 截断；
    - 总长超 ``budget`` 时裁剪顺序：**先丢尾部角色卡 → 再截 SCENE 卡 → SHOT 主体永不截断**。

    Args:
        shot_prompt: 镜头主体提示词（英文）。
        style: 风格代号（如 ``realistic``）。
        character_cards: 角色锁定卡列表；元素可为字符串或 ``(name, card)`` 二元组。
        scene_card: 场景锁定卡（字符串，可空）。
        budget: 总字符预算，默认 :data:`PROMPT_MAX_CHARS`。

    Returns:
        拼接后的单行英文提示词。
    """
    shot = str(shot_prompt or "").strip()
    style_text = str(style or "").strip()
    prefix = f"{VIDEO_STYLE_PREFIX}, {style_text} style" if style_text else VIDEO_STYLE_PREFIX

    cards: list[str] = []
    for item in character_cards or []:
        name, card = _normalize_card(item)
        card = _truncate_card(card)
        if not card:
            continue
        cards.append(f"{name}={card}" if name else card)

    scene = _truncate_card(scene_card)

    def _assemble(char_list: list[str], scene_text: str) -> str:
        parts = [prefix]
        if scene_text:
            parts.append(f"SCENE: {scene_text}")
        if char_list:
            parts.append("CHARACTERS: " + "; ".join(char_list))
        parts.append(f"SHOT: {shot}")
        return " | ".join(parts)

    result = _assemble(cards, scene)
    if len(result) <= budget:
        return result

    # ① 先丢尾部角色卡（逐个），但保留最后一张：把剩余裁剪额度优先留给 SCENE
    while len(cards) > 1 and len(result) > budget:
        cards.pop()
        result = _assemble(cards, scene)
    if len(result) <= budget:
        return result

    # ② 再截 SCENE 卡（按超出量逐步缩短，直至放得下或清空）
    while scene and len(result) > budget:
        over = len(result) - budget
        new_len = len(scene) - max(1, over)
        scene = scene[:new_len].rstrip() if new_len > 0 else ""
        result = _assemble(cards, scene)

    # ③ 若截空 SCENE 后仍放不下，才丢剩余角色卡（含最后一张）
    while cards and len(result) > budget:
        cards.pop()
        result = _assemble(cards, scene)

    # ④ SHOT 主体永不截断（即便仍略超预算也原样返回）
    return result


# 剧情补全（StoryAgent）输出契约：synopsis 承载「完整故事情节」，beats 承载情节节点
_STORY_SCHEMA = """{
  "title": "故事标题（中文）",
  "logline": "一句话梗概：谁 + 想要什么 + 阻碍",
  "synopsis": "完整故事情节（多段正文，至少 2 段，起承转合完整）",
  "setting": "时空/场景总设定（时间、地点、氛围）",
  "characters": [
    {"name": "角色名", "desc": "外貌与性格等固定特征描述"}
  ],
  "beats": [
    {"index": 1, "summary": "该情节节点发生了什么", "purpose": "铺垫|冲突|转折|收尾", "location": "发生地点"}
  ],
  "genre": "题材/类型（如 都市情感、悬疑、科幻温情）",
  "theme": "主题立意",
  "source_dialogue": "原始粗糙对话"
}"""


def story_system_prompt() -> str:
    """StoryAgent 的系统提示词。"""
    return (
        "你是一名专业的影视编剧（StoryAgent），擅长把粗糙的对话创意补全为一个"
        "完整、连贯、可直接改编成短剧的故事情节。你输出的是结构化数据，必须严格遵守以下规则：\n"
        "1) 只输出一个 JSON 对象，不要输出任何解释、标题、Markdown 代码块标记或多余文字；\n"
        "2) 所有文本使用简体中文；\n"
        "3) 字段必须完整，不得缺失；\n"
        "4) synopsis 必须是完整故事情节，至少 2 段，具备起承转合；\n"
        "5) beats 需要 3–6 条，覆盖「起、承、转、合」四个阶段；\n"
        "6) characters 全篇保持命名与设定一致，不新增未出现的角色。"
    )


def story_user_prompt(dialogue: str, style: str) -> str:
    """StoryAgent 的用户提示词。

    Args:
        dialogue: 用户输入的粗糙对话。
        style: 风格代号。

    Returns:
        拼接完成的用户提示词。
    """
    return (
        "请把下面的粗糙对话补全为一个完整、连贯的故事情节。\n\n"
        f"【风格】{style_label(style)}（style 可作为题材 genre 的推导依据）\n\n"
        "【粗糙对话】\n"
        f"{dialogue.strip()}\n\n"
        "【输出 JSON 结构（严格遵守字段名与层级）】\n"
        f"{_STORY_SCHEMA}\n\n"
        "再次强调：只输出上述 JSON 对象本身，不要任何解释文字或代码块围栏。"
    )


def script_system_prompt() -> str:
    """ScriptAgent 的系统提示词。"""
    return (
        "你是一名专业的影视分镜编剧（ScriptAgent），擅长把粗糙的对话创意改写为"
        "可直接拍摄的分镜脚本。你输出的是结构化数据，必须严格遵守以下规则：\n"
        "1) 只输出一个 JSON 对象，不要输出任何解释、标题、Markdown 代码块标记或多余文字；\n"
        "2) 所有文本使用简体中文（shot.prompt 字段使用英文）；\n"
        "3) 字段必须完整，不得缺失；\n"
        "4) shots 数量必须与用户要求的镜头数一致，shot_id 依次为 shot_001、shot_002……；\n"
        "5) 每个 shot 的 duration 使用用户要求的单镜头时长（整数秒）；\n"
        "6) 保证剧情连贯、角色外观在各镜头间保持一致：\n"
        "   - **每个角色**必须产出英文外观锁定卡 lock_card：只描述**不随时间变化**的特征"
        "（发型/脸型/年龄/肤色/服饰款式/体型等），不要写情绪或临时动作；\n"
        "   - **每个场景**必须产出英文场景锁定卡 lock_card：地点/时段/光线/色调/关键陈设；\n"
        "   - scenes 按剧情切分（同地点同时段归并为同一场景），index 从 0 起、连续、不得跳号；\n"
        "   - 每个 shot 必须标 scene_index，指向 scenes 中对应场景；同一场景的镜头 scene_index"
        "连续且相同，换场景时递增。"
    )


def script_user_prompt(dialogue: str, style: str, shot_count: int, shot_duration: int) -> str:
    """ScriptAgent 的用户提示词。

    Args:
        dialogue: 用户输入的粗糙对话。
        style: 风格代号。
        shot_count: 目标镜头数。
        shot_duration: 单镜头时长（秒）。

    Returns:
        拼接完成的用户提示词。
    """
    return (
        "请把下面的粗糙对话改写为一部短剧的分镜脚本。\n\n"
        f"【风格】{style_label(style)}（style 字段填 \"{style}\"）\n"
        f"【镜头数】必须恰好 {shot_count} 个镜头\n"
        f"【单镜头时长】每个镜头 {shot_duration} 秒\n\n"
        "【粗糙对话】\n"
        f"{dialogue.strip()}\n\n"
        "【输出 JSON 结构（严格遵守字段名与层级）】\n"
        f"{_SCRIPT_SCHEMA}\n\n"
        "再次强调：只输出上述 JSON 对象本身，不要任何解释文字或代码块围栏。"
    )


def script_user_prompt_from_story(story, style: str, shot_count: int, shot_duration: int) -> str:
    """由 :class:`StoryDocument` 生成 ScriptAgent 的用户提示词（剧情补全分支）。

    使用 ``synopsis + beats + characters + setting`` 作为分镜拆解依据，
    输出的 ``ScriptDocument.shots[].prompt`` 即为「分镜提示词」。

    Args:
        story: :class:`StoryDocument` 实例（或具同名属性的对象）。
        style: 风格代号。
        shot_count: 目标镜头数。
        shot_duration: 单镜头时长（秒）。

    Returns:
        拼接完成的用户提示词。
    """
    title = str(getattr(story, "title", "") or "未命名故事")
    synopsis = str(getattr(story, "synopsis", "") or "").strip() or "（无剧情正文）"
    setting = str(getattr(story, "setting", "") or "").strip() or "（未指定）"

    characters = list(getattr(story, "characters", []) or [])
    char_lines = "\n".join(
        f"- {getattr(c, 'name', '') or '角色'}：{getattr(c, 'desc', '') or ''}" for c in characters
    ) or "（无）"

    beats = list(getattr(story, "beats", []) or [])
    beat_lines = "\n".join(
        f"{index}. {getattr(b, 'summary', '') or ''}"
        f"（作用：{getattr(b, 'purpose', '') or '—'}，地点：{getattr(b, 'location', '') or '—'}）"
        for index, b in enumerate(beats, start=1)
    ) or "（无）"

    return (
        "请根据下面已补全的完整故事情节，拆解为一部短剧的分镜脚本。\n\n"
        f"【故事标题】{title}\n"
        f"【风格】{style_label(style)}（style 字段填 \"{style}\"）\n"
        f"【镜头数】必须恰好 {shot_count} 个镜头\n"
        f"【单镜头时长】每个镜头 {shot_duration} 秒\n\n"
        f"【场景设定】\n{setting}\n\n"
        f"【角色表】\n{char_lines}\n\n"
        f"【情节节点（起承转合）】\n{beat_lines}\n\n"
        f"【完整故事情节】\n{synopsis}\n\n"
        "【场景切分】请依据上面的「场景设定」与各情节点 location，把全片切分为有序场景"
        "（同地点、同时段归并为同一场景），输出 scenes 数组（index 从 0 起、连续），"
        "并为每个场景给出英文场景锁定卡 lock_card；同时为每个角色给出英文外观锁定卡 lock_card，"
        "每个 shot 通过 scene_index 指向所属场景。\n\n"
        "【输出 JSON 结构（严格遵守字段名与层级）】\n"
        f"{_SCRIPT_SCHEMA}\n\n"
        "再次强调：只输出上述 JSON 对象本身，不要任何解释文字或代码块围栏。"
    )


def json_retry_prompt() -> str:
    """解析失败时的纠正提示（追加到重试对话）。"""
    return (
        "上一次的输出不是合法的 JSON，无法被程序解析。"
        "请重新输出，且**只输出一个合法的 JSON 对象**："
        "不要包含任何解释文字、不要使用 Markdown 代码块围栏、不要包含注释或多余字符。"
    )


# --------------------------------------------------------------------------- #
# CriticAgent 提示词
# --------------------------------------------------------------------------- #
def critic_script_system_prompt() -> str:
    """CriticAgent 脚本评估系统提示词。"""
    return (
        "你是一名严格的影视剧本评审（CriticAgent）。请对给定分镜脚本进行客观评分。"
        "只输出一个 JSON 对象，不要任何解释文字或代码块围栏。评分区间为 1–10（可为小数）。"
    )


def critic_script_user_prompt(script_json: str) -> str:
    """CriticAgent 脚本评估用户提示词。"""
    return (
        "请从以下 4 个维度评估该分镜脚本，并给出 1–10 的分数与一句评语：\n"
        "- coherence：连贯性\n"
        "- shootability：可拍性（画面是否清晰可实现）\n"
        "- character_consistency：角色一致性\n"
        "- plot_completeness：剧情完整度\n\n"
        "【脚本 JSON】\n"
        f"{script_json}\n\n"
        "【输出 JSON 结构】\n"
        "{\n"
        '  "coherence": 8.5,\n'
        '  "shootability": 8.0,\n'
        '  "character_consistency": 8.4,\n'
        '  "plot_completeness": 7.9,\n'
        '  "comments": {"coherence": "一句评语", "shootability": "一句评语", '
        '"character_consistency": "一句评语", "plot_completeness": "一句评语"},\n'
        '  "comment": "对本脚本的一句总评"\n'
        "}\n"
        "只输出该 JSON。"
    )


def critic_video_system_prompt() -> str:
    """CriticAgent 视频评估系统提示词。"""
    return (
        "你是一名视频质量评审（CriticAgent）。请依据脚本与各镜头生成信息，"
        "对视频保真度进行客观评分。只输出一个 JSON 对象，不要任何解释文字或代码块围栏。"
        "评分区间为 1–10（可为小数）。"
    )


def critic_video_user_prompt(script_json: str, shots_summary: str) -> str:
    """CriticAgent 视频评估用户提示词。"""
    return (
        "请从以下 3 个维度评估视频生成保真度，并给出 1–10 的分数与一句评语：\n"
        "- visual_consistency：画面一致性（镜头间画面是否连贯）\n"
        "- dialogue_match：对白匹配（画面/内容与对白是否匹配）\n"
        "- style_fidelity：风格符合度（是否符合指定风格）\n\n"
        "【脚本 JSON】\n"
        f"{script_json}\n\n"
        "【各镜头生成情况】\n"
        f"{shots_summary}\n\n"
        "【输出 JSON 结构】\n"
        "{\n"
        '  "visual_consistency": 7.5,\n'
        '  "dialogue_match": 7.8,\n'
        '  "style_fidelity": 7.4,\n'
        '  "comments": {"visual_consistency": "一句评语", "dialogue_match": "一句评语", '
        '"style_fidelity": "一句评语"},\n'
        '  "comment": "对视频的一句总评"\n'
        "}\n"
        "只输出该 JSON。"
    )


def shots_summary(shots) -> str:
    """把镜头结果整理为供评估模型阅读的简要文本。

    Args:
        shots: ``ShotResult`` 列表。

    Returns:
        多行文本摘要。
    """
    lines: list[str] = []
    for index, shot in enumerate(shots, start=1):
        state = getattr(shot.state, "value", str(shot.state))
        error = getattr(shot, "error", None)
        detail = f"（{error}）" if error else ""
        lines.append(f"{index}. {shot.shot_id}: {state}{detail}")
    return "\n".join(lines) if lines else "（无镜头信息）"


def pretty_json(data) -> str:
    """把对象序列化为便于阅读的 JSON 字符串。"""
    return json.dumps(data, ensure_ascii=False, indent=2)
