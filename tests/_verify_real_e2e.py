"""（非 unittest，供本次独立复核调用）真实远程 LLM 端到端 + 导出哈希复核。

不匹配 ``test_*.py``，不会被 ``unittest discover`` 收集。手动运行：
``.venv/Scripts/python.exe tests/_verify_real_e2e.py``
需要：本机 127.0.0.1:8080 有 OpenAI 兼容服务；config/config.json 指向它。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.utils.ffmpeg_utils import probe_duration  # noqa: E402

PORT = 8132
STAGE_KEYS = ("story", "script", "director", "concat", "critic")


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k.lower() not in (
        "http_proxy", "https_proxy", "all_proxy",
    )}
    env["STORYFORGE_NO_BROWSER"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def _wait_terminal(client: httpx.Client, task_id: str, timeout: float = 600.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/pipeline/{task_id}/status").json().get("data") or {}
        if last.get("state") in ("success", "failed"):
            return last
        time.sleep(1.0)
    return last


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=_clean_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    # 本进程自身访问 127.0.0.1 亦须绕过代理
    client = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=600.0, trust_env=False)
    report: dict = {}
    try:
        for _ in range(160):
            try:
                if client.get("/api/health").status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
            time.sleep(0.5)

        started = time.perf_counter()
        run = client.post("/api/pipeline/run", json={
            "dialogue": "小明：我们把那台旧机器人修好吧。\n小雪：它还能动吗？\n小明：试试看，我负责电路，你负责零件。",
            "style": "realistic", "shot_count": 3, "shot_duration": 2,
            "resolution": "720p", "skip_video_eval": False,
        }).json()
        task_id = (run.get("data") or {}).get("task_id", "")
        status = _wait_terminal(client, task_id)
        elapsed = time.perf_counter() - started

        stages = {k: (v or {}).get("state") for k, v in (status.get("stages") or {}).items()}
        report["claim2_pipeline"] = {
            "task_id": task_id,
            "state": status.get("state"),
            "error": status.get("error"),
            "total_seconds": round(elapsed, 1),
            "stages": stages,
            "all_five_success": all(stages.get(k) == "success" for k in STAGE_KEYS),
            "stage_order_in_status": list(stages.keys()),
            "shot_states": [s.get("state") for s in (status.get("shots") or [])],
            "shot_durations": [s.get("duration") for s in (status.get("shots") or [])],
        }
        story = client.get(f"/api/pipeline/{task_id}/story").json().get("data") or {}
        report["claim2_pipeline"]["story_title"] = story.get("title")
        report["claim2_pipeline"]["story_beats"] = len(story.get("beats") or [])
        report["claim2_pipeline"]["story_synopsis_len"] = len(story.get("synopsis") or "")

        out_dir = ROOT / (status.get("output_dir") or "")
        final = out_dir / "final_video.mp4"

        def _sig(p: Path) -> dict:
            return {
                "md5": hashlib.md5(p.read_bytes()).hexdigest(),
                "size": p.stat().st_size,
                "mtime_ns": p.stat().st_mtime_ns,
            }

        before = _sig(final)
        export = client.post(f"/api/pipeline/{task_id}/export", json={
            "clips": [
                {"shot_id": "shot_003", "enabled": True, "start": 0.0, "end": 1.0, "order": 0},
                {"shot_id": "shot_002", "enabled": False, "order": 1},
                {"shot_id": "shot_001", "enabled": True, "start": 0.5, "end": 1.5, "order": 2},
            ]
        }).json()
        exp = export.get("data") or {}
        export_abs = out_dir / exp.get("export_path", "")
        report["claim3_export"] = {
            "ok": export.get("ok"),
            "clip_count": exp.get("clip_count"),
            "total_duration": exp.get("total_duration"),
            "export_path": exp.get("export_path"),
            "edit_plan_path": exp.get("edit_plan_path"),
            "export_measured_duration": round(probe_duration(str(export_abs)), 3) if export_abs.exists() else None,
            "export_exists": export_abs.exists(),
            "plan_exists": (out_dir / exp.get("edit_plan_path", "")).exists(),
            "download_url": exp.get("download_url"),
        }
        after = _sig(final)
        report["claim3_export"]["final_before"] = before
        report["claim3_export"]["final_after"] = after
        report["claim3_export"]["final_unchanged"] = before == after

        print(json.dumps(report, ensure_ascii=False, indent=2))
        ok = report["claim2_pipeline"]["all_five_success"] and report["claim3_export"]["final_unchanged"]
        return 0 if ok else 1
    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
