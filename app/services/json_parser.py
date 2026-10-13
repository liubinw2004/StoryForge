"""JSON 容错解析：``parse_json_lenient()``。

容错链（见 ARCHITECTURE §8 R1）：
① 提取 ```json 代码块；
② 最大括号匹配（从第一个 ``{``/``[`` 起做配对）；
③ 去尾随逗号 / 修全角引号；
④ **单引号兜底**：标准 ``json.loads`` 仍失败时，用 ``ast.literal_eval`` 解析
   Python 风格字面量（如 ``{'a': 'b'}``，含值内撇号 ``"it's"``），并归一裸
   ``true/false/null`` 后递归转为 JSON 兼容结构。
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

__all__ = ["parse_json_lenient", "extract_json_object"]

_CODE_BLOCK_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)

#: 哨兵：区分「解析失败」与「成功解析为 None」
_UNPARSABLE = object()


def _extract_code_block(text: str) -> str | None:
    """提取 Markdown ```json 代码块内容。"""
    match = _CODE_BLOCK_RE.search(text)
    if match:
        inner = match.group(1).strip()
        return inner or None
    return None


def _extract_balanced(text: str) -> str | None:
    """从文本中提取第一个平衡的 JSON 对象/数组（括号配对，含字符串感知）。

    字符串感知同时识别 ``"`` 与 ``'``，以正确提取**单引号包裹**的 JSON
    （如 ``{'a': "it's ok"}``）。对合法 JSON（仅双引号）行为与之前完全一致。
    """
    start = None
    opener = ""
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            opener = char
            break
    if start is None:
        return None

    closer = "}" if opener == "{" else "]"
    depth = 0
    active_quote = ""
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if active_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == active_quote:
                active_quote = ""
            continue
        if char in "\"'":
            active_quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _cleanup(text: str) -> str:
    """修复常见 LLM 输出瑕疵：全角引号、尾随逗号。"""
    cleaned = text.strip()
    cleaned = (
        cleaned.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    # 去除 } 或 ] 之前的尾随逗号
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


def _replace_bare_literals(text: str) -> str:
    """在**字符串外部**把裸 ``true/false/null`` 换成 ``True/False/None``。

    仅供 :func:`ast.literal_eval` 兜底使用；实现为引号感知扫描，绝不会改动
    引号内部的文本（避免把 ``"it is true"`` 误改成 ``"it is True"``）。
    """
    out: list[str] = []
    active_quote = ""
    index = 0
    length = len(text)
    literals = (("true", "True"), ("false", "False"), ("null", "None"))

    while index < length:
        char = text[index]
        if active_quote:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == active_quote:
                active_quote = ""
            index += 1
            continue

        if char in "\"'":
            active_quote = char
            out.append(char)
            index += 1
            continue

        for literal, replacement in literals:
            if not text.startswith(literal, index):
                continue
            before = text[index - 1] if index > 0 else ""
            after_index = index + len(literal)
            after = text[after_index] if after_index < length else ""
            if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
                continue
            out.append(replacement)
            index += len(literal)
            break
        else:
            out.append(char)
            index += 1

    return "".join(out)


def _to_json_compatible(node: Any) -> Any:
    """把 ``ast.literal_eval`` 的结果递归转换为 JSON 兼容结构。

    Raises:
        ValueError: 出现无法映射为 JSON 的类型（如 set/tuple 以外的自定义对象）。
    """
    if isinstance(node, dict):
        return {str(key): _to_json_compatible(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_to_json_compatible(item) for item in node]
    if node is None or isinstance(node, (str, int, float, bool)):
        return node
    raise ValueError(f"不支持的 Python 字面量类型：{type(node).__name__}")


def _loads_best_effort(text: str) -> Any:
    """先按标准 JSON 解析；失败再按 Python 字面量兜底（支持单引号包裹的 JSON）。

    Args:
        text: 候选文本片段。

    Returns:
        解析结果；全部失败时返回哨兵 :data:`_UNPARSABLE`（以区分「解析为 None」）。
    """
    try:
        return json.loads(text)
    except Exception:
        pass

    stripped = text.strip()
    # Python 字面量兜底仅针对容器（dict/list）：避免把裸 ``None``/``True`` 之类
    # 标量文本由「解析失败」变成「成功」，从而改变既有语义。
    if not stripped.startswith(("{", "[")):
        return _UNPARSABLE

    for variant in (stripped, _replace_bare_literals(stripped)):
        try:
            return _to_json_compatible(ast.literal_eval(variant))
        except (ValueError, SyntaxError):
            continue
    return _UNPARSABLE


def extract_json_object(text: str) -> str | None:
    """仅做「提取」不解析，返回可能的 JSON 片段（供调试/提示词使用）。"""
    if not text:
        return None
    block = _extract_code_block(text)
    if block:
        return block
    return _extract_balanced(text)


def parse_json_lenient(text: str) -> Any:
    """尽可能宽松地把模型输出解析为 Python 对象。

    依次尝试：代码块 → 括号匹配 → 清洗（全角引号/尾随逗号）→
    Python 字面量兜底（单引号 JSON）。

    Args:
        text: 模型原始输出文本。

    Returns:
        解析得到的对象（通常为 dict）。

    Raises:
        ValueError: 所有策略均无法解析出合法 JSON。
    """
    if not text or not text.strip():
        raise ValueError("模型返回内容为空")

    raw = text.strip()
    candidates: list[str] = []

    block = _extract_code_block(raw)
    if block:
        candidates.append(block)

    balanced = _extract_balanced(raw)
    if balanced:
        candidates.append(balanced)

    candidates.append(raw)

    for candidate in candidates:
        for variant in (candidate, _cleanup(candidate)):
            parsed = _loads_best_effort(variant)
            if parsed is not _UNPARSABLE:
                return parsed
        # 候选块内再做一次括号匹配 + 清洗
        nested = _extract_balanced(candidate)
        if nested:
            parsed = _loads_best_effort(_cleanup(nested))
            if parsed is not _UNPARSABLE:
                return parsed

    raise ValueError("无法从模型输出中解析出合法 JSON")
