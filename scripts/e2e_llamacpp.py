#!/usr/bin/env python3
"""端到端验证：用远程 GPU 服务器上的 llama.cpp（OpenAI 兼容 API）跑一次完整流水线。

流程：
    1. 备份 ``config/config.json``；
    2. 写入「OpenAI 兼容 + llama.cpp」配置（base_url 默认 ``http://127.0.0.1:8080/v1``）；
    3. 拉起 StoryForge 应用（uvicorn，默认 127.0.0.1:8131）；
    4. ``POST /api/pipeline/run`` 跑 3 镜头 × 2 秒（Mock 视频）；
    5. 打印 ScriptAgent 产出的中文脚本与 CriticAgent 评分；
    6. 还原配置、关闭应用。

用法::

    .venv/Scripts/python.exe scripts/e2e_llamacpp.py
    .venv/Scripts/python.exe scripts/e2e_llamacpp.py --base-url http://127.0.0.1:8080/v1 --model qwen3-30b-a3b
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"


def _write_config(
    base_url: str, model: str, api_key: str, shot_count: int, shot_duration: int, llm_mode: str
) -> dict:
    """写入目标 LLM 配置，返回原配置（供还原）。"""
    previous = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = {
        "llm_mode": llm_mode,
        "llm_base_url": base_url,
        "llm_api_key": api_key,
        "llm_model": model,
        "video_provider": "mock",
        "video_api_key": "",
        "video_base_url": "",
        "video_model": "",
        "resolution": "720p",
        "style": "realistic",
        "shot_count": shot_count,
        "shot_duration": shot_duration,
        "skip_video_eval": False,
    }
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return previous


def _wait_terminal(client: httpx.Client, task_id: str, timeout: float = 600.0) -> dict:
    """轮询任务状态直到终态。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        res = client.get(f"/api/pipeline/{task_id}/status").json()
        last = res.get("data") or {}
        if last.get("state") in ("success", "failed"):
            return last
        time.sleep(1.0)
    return last


def main() -> int:
    """入口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model", default="qwen3-30b-a3b")
    parser.add_argument("--api-key", default="sk-local-llamacpp")
    parser.add_argument("--llm-mode", default="openai", choices=["openai", "ollama"])
    parser.add_argument("--port", type=int, default=8131)
    parser.add_argument("--shot-count", type=int, default=3)
    parser.add_argument("--shot-duration", type=int, default=2)
    parser.add_argument(
        "--dialogue",
        default="小明：我们把那台旧机器人修好吧。\n小雪：它还能动吗？\n小明：试试看，我负责电路，你负责零件。",
    )
    args = parser.parse_args()

    previous = _write_config(
        args.base_url, args.model, args.api_key, args.shot_count, args.shot_duration, args.llm_mode
    )
    proc: subprocess.Popen | None = None
    try:
        print(f"[1/4] 启动 StoryForge 应用 (127.0.0.1:{args.port}) ...")
        env = dict(os.environ, STORYFORGE_NO_BROWSER="1")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "warning"],
            cwd=str(PROJECT_ROOT), env=env,
        )
        client = httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=600.0)
        for _ in range(120):
            try:
                if client.get("/api/health").status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)

        print("[2/4] 测试 LLM 连通性 ...")
        test = client.post("/api/config/test-llm", json=json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        print("     ", json.dumps(test.json().get("data"), ensure_ascii=False))

        print("[3/4] 运行流水线（真实远程 LLM + Mock 视频）...")
        started = time.perf_counter()
        run = client.post("/api/pipeline/run", json={
            "dialogue": args.dialogue,
            "style": "realistic",
            "shot_count": args.shot_count,
            "shot_duration": args.shot_duration,
            "resolution": "720p",
            "skip_video_eval": False,
        }).json()
        task_id = (run.get("data") or {}).get("task_id", "")
        print(f"      task_id={task_id}")
        status = _wait_terminal(client, task_id)
        elapsed = time.perf_counter() - started
        print(f"      state={status.get('state')} 总耗时={elapsed:.1f}s error={status.get('error')}")
        for name, stage in (status.get("stages") or {}).items():
            print(f"      stage {name:9s} {stage.get('state'):8s} {stage.get('elapsed', 0):.1f}s")

        print("[4/4] 产出 ...")
        script = client.get(f"/api/pipeline/{task_id}/script").json().get("data") or {}
        print("\n===== ScriptAgent（远程 llama.cpp 生成）=====")
        print(json.dumps(script, ensure_ascii=False, indent=2)[:2500])
        report = client.get(f"/api/pipeline/{task_id}/report").json().get("data") or {}
        print("\n===== CriticAgent 评分 =====")
        print(json.dumps(report, ensure_ascii=False, indent=2)[:1500])
        return 0 if status.get("state") == "success" else 1
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
        CONFIG_PATH.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n[ok] 配置已还原，应用已关闭")


if __name__ == "__main__":
    sys.exit(main())
