#!/usr/bin/env python3
"""LLM 推理服务基准测试脚本（OpenAI 兼容 / Ollama）。

在服务器或本机（经 SSH 隧道）运行均可。默认打向
``http://127.0.0.1:11434`` 的 Ollama 服务。

测量指标：
    - 首 token 延迟 (TTFT, ms)
    - 输出 token 数
    - 解码速度 (tokens/s)
    - 端到端总耗时 (s)

用法::

    python3 bench_llm.py                      # 默认模型 qwen3:30b-a3b
    python3 bench_llm.py --model qwen2.5:14b
    python3 bench_llm.py --base http://127.0.0.1:11434 --model qwen3:30b-a3b

依赖：仅标准库（urllib）。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

# 固定评测提示词：要求输出一段结构化的中文内容，便于稳定对比。
PROMPT = (
    "请用中文写一段约 200 字的短故事开头，主题是："
    "一位工程师在深夜的机房里调试一台老旧的 GPU 服务器。"
    "要求语言流畅、有画面感。"
)


def bench(base: str, model: str, max_tokens: int = 256, temperature: float = 0.7) -> dict:
    """执行一次流式对话并统计性能指标。

    Args:
        base: 服务基地址，如 ``http://127.0.0.1:11434``。
        model: 模型名。
        max_tokens: 最大生成 token 数。
        temperature: 采样温度。

    Returns:
        含 ttft_ms / completion_tokens / tokens_per_sec / total_s 的字典。
    """
    url = f"{base.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        method="POST",
    )

    start = time.perf_counter()
    ttft_ms: float | None = None           # 首个「内容」token 延迟
    first_any_ms: float | None = None      # 首个任意 token（含思考）延迟
    content_tokens = 0
    reasoning_tokens = 0
    text_parts: list[str] = []

    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
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
            rpiece = delta.get("reasoning") or ""
            cpiece = delta.get("content") or ""
            if rpiece or cpiece:
                if first_any_ms is None:
                    first_any_ms = (time.perf_counter() - start) * 1000.0
            if rpiece:
                reasoning_tokens += 1
            if cpiece:
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - start) * 1000.0
                content_tokens += 1
                text_parts.append(cpiece)

    total_s = time.perf_counter() - start
    total_tokens = content_tokens + reasoning_tokens
    tps = total_tokens / total_s if total_s > 0 else 0.0

    return {
        "model": model,
        "first_token_ms": round(first_any_ms or 0.0, 1),
        "ttft_content_ms": round(ttft_ms or 0.0, 1),
        "content_tokens": content_tokens,
        "reasoning_tokens": reasoning_tokens,
        "tokens_per_sec": round(tps, 2),
        "total_s": round(total_s, 2),
        "sample": "".join(text_parts)[:60],
    }


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="LLM 基准测试")
    parser.add_argument("--base", default="http://127.0.0.1:11434", help="服务基地址")
    parser.add_argument("--model", default="qwen3:30b-a3b", help="模型名")
    parser.add_argument("--max-tokens", type=int, default=256, help="最大生成 token 数")
    parser.add_argument("--runs", type=int, default=2, help="重复次数（取均值）")
    args = parser.parse_args()

    results = []
    for i in range(args.runs):
        print(f"[run {i + 1}/{args.runs}] {args.model} ...", flush=True)
        try:
            r = bench(args.base, args.model, args.max_tokens)
        except Exception as exc:  # noqa: BLE001 - 基准脚本，吞异常便于批量跑
            print(f"  ERROR: {exc}")
            return
        print(f"  {json.dumps(r, ensure_ascii=False)}")
        results.append(r)

    if results:
        avg_ttft = sum(r["first_token_ms"] for r in results) / len(results)
        avg_tps = sum(r["tokens_per_sec"] for r in results) / len(results)
        print(
            f"\n==> {args.model}: avg 首token(任意)={avg_ttft:.0f} ms, "
            f"avg 生成速度={avg_tps:.1f} tokens/s"
        )


if __name__ == "__main__":
    main()
