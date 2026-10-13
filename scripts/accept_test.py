#!/usr/bin/env python3
"""StoryForge 真实场景验收测试：模拟 ScriptAgent 的 JSON 生成。

用与 StoryForge ``script_system_prompt``/``script_user_prompt`` 相同的提示词，
请求模型输出分镜 JSON，检验：
    1. 端到端耗时（秒）；
    2. content 是否为合法 JSON；
    3. 是否含必需字段（title / shots）；
    4. 首 token 延迟。

用法::

    python3 accept_test.py --model qwen3:30b-a3b
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

SYSTEM = (
    "你是一名专业的影视分镜编剧（ScriptAgent），擅长把粗糙的对话创意改写为"
    "可直接拍摄的分镜脚本。你输出的是结构化数据，必须严格遵守以下规则：\n"
    "1) 只输出一个 JSON 对象，不要输出任何解释、标题、Markdown 代码块标记或多余文字；\n"
    "2) 所有文本使用简体中文（shot.prompt 字段使用英文）；\n"
    "3) 字段必须完整，不得缺失；\n"
    "4) shots 数量必须与用户要求的镜头数一致，shot_id 依次为 shot_001、shot_002……；\n"
    "5) 每个 shot 的 duration 使用用户要求的单镜头时长（整数秒）；\n"
    "6) 保证剧情连贯、角色外观在各镜头间保持一致。"
)

USER = (
    "原始对话：\n"
    "甲：这家店的咖啡真不错。\n"
    "乙：是啊，就是有点贵。\n"
    "要求：生成 3 个镜头的分镜脚本，每个镜头 4 秒，风格 realistic。\n\n"
    "输出 JSON："
)

SCHEMA_KEYS = {"title", "shots"}


def _strip_md(text: str) -> str:
    """去掉可能的 markdown 代码块围栏。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
        elif "```" in t:
            t = t[: t.rindex("```")]
    return t.strip()


def run(base: str, model: str) -> None:
    """执行一次 StoryForge 场景验收。"""
    url = f"{base.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
        ],
        "temperature": 0.75,
        "stream": True,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        method="POST",
    )
    start = time.perf_counter()
    first_ms = None
    content = []
    reasoning_chars = 0
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            r = delta.get("reasoning") or ""
            c = delta.get("content") or ""
            if (r or c) and first_ms is None:
                first_ms = (time.perf_counter() - start) * 1000.0
            reasoning_chars += len(r)
            if c:
                content.append(c)
    total = time.perf_counter() - start

    text = "".join(content)
    ok_json = False
    keys_ok = False
    parsed = None
    try:
        parsed = json.loads(_strip_md(text))
        ok_json = True
        keys_ok = isinstance(parsed, dict) and SCHEMA_KEYS.issubset(parsed.keys())
    except Exception as exc:  # noqa: BLE001
        print(f"    JSON 解析失败: {exc}")

    print(f"  model={model}")
    print(f"    首token延迟       : {first_ms:.0f} ms")
    print(f"    思考内容字符数    : {reasoning_chars}")
    print(f"    答案字符数        : {len(text)}")
    print(f"    总耗时            : {total:.1f} s")
    print(f"    JSON 合法         : {ok_json}")
    print(f"    必需字段完整      : {keys_ok}")
    if parsed is not None:
        shots = parsed.get("shots") if isinstance(parsed, dict) else None
        n = len(shots) if isinstance(shots, list) else 0
        print(f"    shots 数量        : {n}")


def main() -> None:
    """入口。"""
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:11434")
    p.add_argument("--model", required=True)
    args = p.parse_args()
    print(f"[StoryForge 场景验收] {args.model}")
    run(args.base, args.model)


if __name__ == "__main__":
    main()
