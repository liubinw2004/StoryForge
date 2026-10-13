"""端到端冒烟测试（HTTP 层，需真实运行的服务 + 桩 LLM 服务）。

前置：
- 服务已启动（默认 ``http://127.0.0.1:8123``，可用 ``SF_BASE_URL`` 覆盖）；
- 桩 LLM 服务已启动（默认 127.0.0.1:18080）；
- ``config/config.json`` 已指向桩（``llm_mode=openai``）。

覆盖两条路径：
- 旧路径（不传 ``review``）→ 直达 success（B2）；
- 人审路径（``review=true``）→ 挂起 ``awaiting_review`` → ``confirm-script`` 原地续跑（B5）。

运行：``.venv/Scripts/python.exe tests/e2e_smoke.py``
退出码 = 失败用例数（0 表示全通过）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE_URL = os.environ.get("SF_BASE_URL", "http://127.0.0.1:8123")
CONFIG_PATH = Path(os.environ.get("SF_CONFIG", r"E:\ai\storyforge\config\config.json"))
EXPECTED_MASK = os.environ.get("SF_EXPECTED_MASK", "")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条用例结果。"""
    RESULTS.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name} {('- ' + detail) if detail else ''}", flush=True)
    return ok


def _ffprobe_duration(path: str) -> float:
    """用 ffprobe 读取时长。"""
    exe = "ffprobe"
    proc = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return 0.0


def _raw_frame(path: str, ts: float) -> bytes:
    """抽取指定时间的原始 RGB 帧。"""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(ts), "-i", path,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=90,
    )
    return proc.stdout


def _diff_ratio(a: bytes, b: bytes) -> float:
    if not a or not b:
        return 1.0
    n = min(len(a), len(b))
    diff = sum(1 for i in range(0, n, 3) if a[i:i + 3] != b[i:i + 3])
    return diff / max(1, n // 3)


def _wait_terminal(client: httpx.Client, task_id: str, timeout: float = 240.0) -> dict:
    """轮询 /status 直到终态。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        res = client.get(f"/api/pipeline/{task_id}/status").json()
        last = res.get("data") or {}
        if last.get("state") in ("success", "failed"):
            return last
        time.sleep(1.0)
    return last


def _wait_state(client: httpx.Client, task_id: str, target: str, timeout: float = 120.0) -> dict:
    """轮询 /status 直到到达指定状态（如 ``awaiting_review``）或超时。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        res = client.get(f"/api/pipeline/{task_id}/status").json()
        last = res.get("data") or {}
        if last.get("state") == target or last.get("state") in ("success", "failed"):
            return last
        time.sleep(0.5)
    return last


def _wait_director_running(client: httpx.Client, task_id: str, timeout: float = 120.0) -> dict:
    """轮询 /status 直到 director 阶段进入 running（等待上传初始图）。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        res = client.get(f"/api/pipeline/{task_id}/status").json()
        last = res.get("data") or {}
        director = (last.get("stages") or {}).get("director") or {}
        if director.get("state") == "running":
            return last
        if last.get("state") in ("success", "failed"):
            return last
        time.sleep(0.5)
    return last


def _drive_chain(client: httpx.Client, task_id: str, seg_count: int, timeout: float = 240.0) -> tuple[list[str], bool]:
    """逐段等待 ``awaiting_confirm`` 并「接受」，返回 (各段终态, 是否全部接受成功)。"""
    deadline = time.time() + timeout
    states: list[str] = []
    for index in range(1, seg_count + 1):
        seg: dict = {}
        while time.time() < deadline:
            data = client.get(f"/api/pipeline/{task_id}/chain").json().get("data") or {}
            segments = (data.get("chain") or {}).get("segments") or []
            seg = next((s for s in segments if s.get("index") == index), {})
            if seg.get("state") in ("awaiting_confirm", "failed", "accepted"):
                break
            time.sleep(0.5)
        state = seg.get("state")
        states.append(state)
        if state == "awaiting_confirm":
            client.post(f"/api/pipeline/{task_id}/chain/segments/{index}/accept", json={})
        elif state == "failed":
            return states, False
        # accepted：直接进入下一段
    return states, all(s == "awaiting_confirm" for s in states)


def main() -> int:
    """执行全部 E2E 检查。"""
    client = httpx.Client(base_url=BASE_URL, timeout=300.0, follow_redirects=False)

    # ---------- B1 静态页 ---------- #
    try:
        r = client.get("/")
        html = r.text
        check("GET / 返回 200 且为 StoryForge 页面", r.status_code == 200 and "StoryForge" in html,
              f"HTTP {r.status_code}")
        check("GET /static/style.css 返回 200", client.get("/static/style.css").status_code == 200)
        check("GET /static/app.js 返回 200", client.get("/static/app.js").status_code == 200)
        # 零外网引用（除占位/提示文本）
        import re as _re
        external = [
            m for m in _re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html)
            if m.startswith("http") or m.startswith("//")
        ]
        check("index.html 无外链 script/link", not external, f"外链={external}")
    except Exception as exc:  # noqa: BLE001
        check("静态页检查", False, repr(exc))

    # ---------- B1 health ---------- #
    health = client.get("/api/health").json()
    data = health.get("data") or {}
    check("GET /api/health 统一响应且字段齐全",
          health.get("ok") is True
          and {"status", "port", "ffmpeg", "ffmpeg_version", "python"} <= set(data),
          json.dumps(data, ensure_ascii=False))
    check("health.ffmpeg == true", data.get("ffmpeg") is True, f"ffmpeg={data.get('ffmpeg')}")
    check("health.ffmpeg_version 有版本号", bool(data.get("ffmpeg_version")),
          str(data.get("ffmpeg_version")))

    # ---------- C 配置脱敏回填（MASK_ROUNDTRIP） ---------- #
    cfg = client.get("/api/config").json().get("data") or {}
    masked = cfg.get("llm_api_key", "")
    check("GET /api/config 返回脱敏 key", "****" in masked and masked != "",
          f"llm_api_key={masked}")
    if EXPECTED_MASK:
        check("脱敏值符合 sk-****xxxx 形态", masked == EXPECTED_MASK, f"{masked} vs {EXPECTED_MASK}")
    # 回填脱敏值保存
    save_res = client.post("/api/config", json=cfg).json()
    check("POST /api/config 回填脱敏值成功", save_res.get("ok") is True)
    on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    check("磁盘保留真实 key（脱敏值未覆盖）",
          on_disk.get("llm_api_key", "").startswith("sk-") and "****" not in on_disk.get("llm_api_key", ""),
          f"disk llm_api_key={on_disk.get('llm_api_key')}")

    # ---------- D 参数校验 422 ---------- #
    bad = client.post("/api/pipeline/run", json={"dialogue": "", "shot_count": 99})
    check("空 dialogue / shot_count=99 → 422 CFG_INVALID",
          bad.status_code == 422 and "CFG_INVALID" in (bad.json().get("error") or ""),
          f"HTTP {bad.status_code} {bad.text[:120]}")
    bad_provider = client.post("/api/config", json={"video_provider": "not-a-provider"})
    check("非法 provider → 422 CFG_INVALID",
          bad_provider.status_code == 422 and "CFG_INVALID" in (bad_provider.json().get("error") or ""),
          f"HTTP {bad_provider.status_code}")

    # ---------- D LLM 不可达（服务不崩） ---------- #
    unreachable_cfg = dict(cfg)
    unreachable_cfg.update({"llm_mode": "openai", "llm_base_url": "http://127.0.0.1:9/v1",
                            "llm_api_key": "stub", "llm_model": "m"})
    tr = client.post("/api/config/test-llm", json=unreachable_cfg)
    tr_data = tr.json().get("data") or {}
    check("test-llm 不可达 → data.ok=false + 中文 LLM_UNREACHABLE",
          tr.json().get("ok") is True and tr_data.get("ok") is False
          and "LLM_UNREACHABLE" in (tr_data.get("message") or ""),
          str(tr_data.get("message"))[:120])

    # ---------- B2 完整流水线（桩 LLM + Mock 视频 + 串行首帧链） ---------- #
    run = client.post("/api/pipeline/run", json={
        "dialogue": "小明和小红在咖啡馆相遇，讨论要不要一起创业。",
        "style": "realistic", "shot_count": 3, "shot_duration": 2,
        "resolution": "720p", "skip_video_eval": False,
    }).json()
    check("POST /api/pipeline/run 立即返回 task_id",
          run.get("ok") is True and bool((run.get("data") or {}).get("task_id")),
          json.dumps(run.get("data"), ensure_ascii=False))
    task_id = (run.get("data") or {}).get("task_id", "")

    director_wait = _wait_director_running(client, task_id)
    check("流水线进入 director 等待初始图",
          (director_wait.get("stages") or {}).get("director", {}).get("state") == "running",
          f"state={director_wait.get('state')} error={director_wait.get('error')}")

    # 上传初始图启动串行首帧链（mock 下不读图片内容，仅需非空字节）
    png = b"\x89PNG\r\n\x1a\n" + b"E2E_INITIAL_FRAME" * 16
    chain_start = client.post(
        f"/api/pipeline/{task_id}/chain/start",
        files={"file": ("initial.png", png, "image/png")},
    ).json()
    check("POST /chain/start 初始化串行首帧链",
          chain_start.get("ok") is True and bool((chain_start.get("data") or {}).get("chain")),
          json.dumps(chain_start.get("data"), ensure_ascii=False))
    seg_count = len(((chain_start.get("data") or {}).get("chain") or {}).get("segments") or [])
    check("链段数 == 3", seg_count == 3, f"segments={seg_count}")

    chain_states, chain_ok = _drive_chain(client, task_id, seg_count)
    check("逐段确认全部接受（start→accept→concat）", chain_ok,
          f"states={chain_states}")

    status = _wait_terminal(client, task_id)
    check("流水线到达终态 success", status.get("state") == "success",
          f"state={status.get('state')} error={status.get('error')}")
    stages = status.get("stages") or {}
    stage_states = {k: (v or {}).get("state") for k, v in stages.items()}
    check("五阶段全部 success",
          all(stage_states.get(k) == "success"
              for k in ("story", "script", "director", "concat", "critic")),
          json.dumps(stage_states, ensure_ascii=False))
    shots = status.get("shots") or []
    check("3 个镜头全部 success",
          len(shots) == 3 and all(s.get("state") == "success" for s in shots),
          f"shots={[s.get('state') for s in shots]}")
    check("镜头产物使用串行链命名（shots/segment_001.mp4）",
          bool(shots) and shots[0].get("video_path") == "shots/segment_001.mp4",
          f"video_path={shots[0].get('video_path') if shots else None}")

    # ---------- B2 剧情补全产物 ---------- #
    story = client.get(f"/api/pipeline/{task_id}/story").json()
    story_data = story.get("data") or {}
    check("GET /story 返回剧情（synopsis 非空、beats ≥ 3）",
          story.get("ok") is True and bool(story_data.get("synopsis"))
          and len(story_data.get("beats", [])) >= 3,
          f"title={story_data.get('title')} beats={len(story_data.get('beats', []))}")
    check("脚本 shots[].prompt（分镜提示词）非空",
          bool(((client.get(f"/api/pipeline/{task_id}/script").json().get("data") or {})
                .get("shots") or [{}])[0].get("prompt")))
    r_story_art = client.get(f"/api/pipeline/{task_id}/download-artifact",
                             params={"name": "story.json"})
    check("GET /download-artifact(story.json) → 200 attachment",
          r_story_art.status_code == 200
          and "attachment" in r_story_art.headers.get("content-disposition", ""),
          f"HTTP {r_story_art.status_code}")

    # ---------- B2 产物接口 ---------- #
    script = client.get(f"/api/pipeline/{task_id}/script").json()
    check("GET /script 返回 3 镜头脚本",
          script.get("ok") is True and len((script.get("data") or {}).get("shots", [])) == 3)
    report = client.get(f"/api/pipeline/{task_id}/report").json().get("data") or {}
    script_score = report.get("script") or {}
    video_score = report.get("video") or {}
    check("评估报告含脚本 4 维 + 视频 3 维，未跳过",
          all(k in script_score for k in ("coherence", "shootability", "character_consistency", "plot_completeness", "total"))
          and all(k in video_score for k in ("visual_consistency", "dialogue_match", "style_fidelity", "total"))
          and report.get("video_skipped") is False,
          f"script_total={script_score.get('total')} video_total={video_score.get('total')}")

    r_final = client.get(f"/api/pipeline/{task_id}/video/final_video.mp4")
    check("GET /video/final_video.mp4 → 200 video/mp4",
          r_final.status_code == 200 and "video/mp4" in r_final.headers.get("content-type", ""),
          f"HTTP {r_final.status_code}")
    r_shot = client.get(f"/api/pipeline/{task_id}/video/shots/segment_001.mp4")
    check("GET /video/shots/segment_001.mp4 → 200", r_shot.status_code == 200, f"HTTP {r_shot.status_code}")
    r_frame = client.get(f"/api/pipeline/{task_id}/frames/tail_001.png")
    check("GET /frames/tail_001.png → 200", r_frame.status_code == 200, f"HTTP {r_frame.status_code}")
    r_dl = client.get(f"/api/pipeline/{task_id}/download")
    check("GET /download → 200 attachment",
          r_dl.status_code == 200 and "attachment" in r_dl.headers.get("content-disposition", ""),
          r_dl.headers.get("content-disposition", ""))
    r_art = client.get(f"/api/pipeline/{task_id}/download-artifact", params={"name": "script.json"})
    check("GET /download-artifact(script.json) → 200 attachment",
          r_art.status_code == 200 and "attachment" in r_art.headers.get("content-disposition", ""),
          f"HTTP {r_art.status_code}")

    # ---------- B3 独立验证 Mock 视频规格 ---------- #
    tmp_dir = Path(os.environ.get("TEMP", "/tmp")) / "sf_e2e_frames"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    final_file = tmp_dir / "final_video.mp4"
    final_file.write_bytes(r_final.content)
    dur = _ffprobe_duration(str(final_file))
    check("拼接时长 ≈ Σ镜头时长（3×2=6s，容差 ≤0.5s）", abs(dur - 6.0) <= 0.5, f"实测 {dur:.2f}s")

    # 分辨率/编码/帧率
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height,r_frame_rate",
         "-of", "default=nw=1", str(final_file)],
        capture_output=True, text=True, timeout=60,
    ).stdout
    check("成片规格 1280x720/24fps/h264",
          "codec_name=h264" in probe and "width=1280" in probe
          and "height=720" in probe and "r_frame_rate=24/1" in probe,
          probe.replace("\n", " "))

    # 进度条：抽 2 帧确认不同
    early = _raw_frame(str(final_file), 0.2)
    late = _raw_frame(str(final_file), 1.8)
    ratio = _diff_ratio(early, late)
    check("进度条逐帧增长（0.2s 与 1.8s 帧不同）", ratio > 0.01, f"差异比例={ratio:.4f}")

    # ---------- B4 剪辑导出（EDL：乱序 + 禁用 + 裁剪） ---------- #
    import hashlib as _hashlib
    final_before = client.get(f"/api/pipeline/{task_id}/video/final_video.mp4").content
    final_hash_before = _hashlib.md5(final_before).hexdigest()
    export = client.post(f"/api/pipeline/{task_id}/export", json={
        "clips": [
            {"shot_id": "shot_003", "enabled": True, "start": 0.0, "end": 1.0, "order": 0},
            {"shot_id": "shot_002", "enabled": False, "order": 1},
            {"shot_id": "shot_001", "enabled": True, "start": 0.5, "end": 1.5, "order": 2},
        ]
    }).json()
    exp = export.get("data") or {}
    check("POST /export 返回 export_path / edit_plan_path（配对命名）",
          export.get("ok") is True
          and str(exp.get("export_path", "")).startswith("exports/export_")
          and str(exp.get("edit_plan_path", "")).startswith("exports/edit_plan_")
          and exp.get("edit_plan_path", "").split("edit_plan_")[-1]
          == exp.get("export_path", "").split("export_")[-1].rsplit(".", 1)[0] + ".json",
          json.dumps(exp, ensure_ascii=False))
    check("导出 clip_count == 2（禁用的 shot_002 被排除）",
          exp.get("clip_count") == 2, f"clip_count={exp.get('clip_count')}")
    check("导出采用任务相对正斜杠路径（无 E:\\ 绝对路径）",
          "E:" not in str(exp.get("export_path", ""))
          and not str(exp.get("export_path", "")).startswith("/"),
          str(exp.get("export_path")))
    exp_path = str(exp.get("export_path", ""))
    if exp_path:
        r_exp = client.get(f"/api/pipeline/{task_id}/video/{exp_path}")
        check("GET /video/<export_path> → 200 video/mp4",
              r_exp.status_code == 200 and "video/mp4" in r_exp.headers.get("content-type", ""),
              f"HTTP {r_exp.status_code}")
    final_after = client.get(f"/api/pipeline/{task_id}/video/final_video.mp4").content
    check("导出未改动 final_video.mp4（内容哈希一致）",
          _hashlib.md5(final_after).hexdigest() == final_hash_before)

    # 全部禁用 → SYS_BAD_REQUEST
    export_none = client.post(f"/api/pipeline/{task_id}/export", json={
        "clips": [
            {"shot_id": "shot_001", "enabled": False},
            {"shot_id": "shot_002", "enabled": False},
            {"shot_id": "shot_003", "enabled": False},
        ]
    }).json()
    check("全部禁用 → ok=false SYS_BAD_REQUEST",
          export_none.get("ok") is False and "SYS_BAD_REQUEST" in (export_none.get("error") or ""),
          str(export_none.get("error"))[:120])

    # ---------- D 错误与安全 ---------- #
    nonexist = client.get("/api/pipeline/nonexistent-task/status").json()
    check("任务不存在 → ok=false TASK_NOT_FOUND",
          nonexist.get("ok") is False and "TASK_NOT_FOUND" in (nonexist.get("error") or ""),
          str(nonexist.get("error"))[:100])

    # 路径穿越（保留 %2e%2e%2f 编码，交由 Starlette 解码）
    trav = client.get(f"/api/pipeline/{task_id}/video/..%2f..%2f..%2fconfig%2fconfig.json")
    check("路径穿越 video/..%2f.. → 400（拒绝，且非 200）",
          trav.status_code == 400, f"HTTP {trav.status_code} body={trav.text[:100]}")

    missing_frame = client.get(f"/api/pipeline/{task_id}/frames/frame_999.jpg")
    check("不存在的帧 → 404", missing_frame.status_code == 404, f"HTTP {missing_frame.status_code}")

    non_mp4 = client.get(f"/api/pipeline/{task_id}/video/script.json")
    check("video 端点访问非 mp4 → 400", non_mp4.status_code == 400, f"HTTP {non_mp4.status_code}")

    # ---------- D 并发隔离 ---------- #
    ids = []
    for _ in range(2):
        res = client.post("/api/pipeline/run", json={
            "dialogue": "并发任务测试对话内容", "shot_count": 2, "shot_duration": 2,
        }).json()
        ids.append((res.get("data") or {}).get("task_id"))
    # 各自进入 director 等待初始图（此时不应互相污染）
    for tid in ids:
        _wait_director_running(client, tid)
    # 各自上传初始图并逐段确认
    for tid in ids:
        png_c = b"\x89PNG\r\n\x1a\n" + b"CONCURRENT_FRAME" * 8
        cs = client.post(
            f"/api/pipeline/{tid}/chain/start",
            files={"file": ("initial.png", png_c, "image/png")},
        ).json()
        sc = len(((cs.get("data") or {}).get("chain") or {}).get("segments") or [])
        _drive_chain(client, tid, sc)
    states = [_wait_terminal(client, tid).get("state") for tid in ids]
    check("并发 2 个任务互不干扰且均 success",
          len(set(ids)) == 2 and all(s == "success" for s in states),
          f"ids={ids} states={states}")

    # ---------- B5 分镜确认区（review=true 人审门 + 原地续跑） ---------- #
    run_review = client.post("/api/pipeline/run", json={
        "dialogue": "小红在雨夜便利店遇到旧友，两人决定重开当年关闭的书店。",
        "style": "realistic", "shot_count": 2, "shot_duration": 2,
        "resolution": "720p", "skip_video_eval": True, "review": True,
    }).json()
    check("review=true：POST /run 立即返回 task_id",
          run_review.get("ok") is True and bool((run_review.get("data") or {}).get("task_id")),
          json.dumps(run_review.get("data"), ensure_ascii=False))
    review_id = (run_review.get("data") or {}).get("task_id", "")

    waited = _wait_state(client, review_id, "awaiting_review")
    rv_state = waited.get("state")
    check("review=true：脚本后挂起 awaiting_review（非终态）",
          rv_state == "awaiting_review", f"state={rv_state} error={waited.get('error')}")
    check("review=true：挂起时无镜头结果（shots 为空）",
          (waited.get("shots") or []) == [], f"shots={waited.get('shots')}")
    rv_stages = {k: (v or {}).get("state") for k, v in (waited.get("stages") or {}).items()}
    check("review=true：story/script 已 success，director 仍 pending",
          rv_stages.get("story") == "success" and rv_stages.get("script") == "success"
          and rv_stages.get("director") == "pending",
          json.dumps(rv_stages, ensure_ascii=False))
    rv_final = client.get(f"/api/pipeline/{review_id}/video/final_video.mp4")
    check("review=true：挂起时 final_video.mp4 尚不存在（非 200）",
          rv_final.status_code != 200, f"HTTP {rv_final.status_code}")

    # 非 awaiting_review 状态确认（用已 success 的主任务）→ TASK_STATE_INVALID
    bad_confirm = client.post(f"/api/pipeline/{task_id}/confirm-script", json={}).json()
    check("对非待确认任务 confirm-script → TASK_STATE_INVALID",
          bad_confirm.get("ok") is False and "TASK_STATE_INVALID" in (bad_confirm.get("error") or ""),
          str(bad_confirm.get("error"))[:120])
    nonexist_confirm = client.post(
        "/api/pipeline/nonexistent-task/confirm-script", json={}).json()
    check("不存在的任务 confirm-script → TASK_NOT_FOUND",
          nonexist_confirm.get("ok") is False
          and "TASK_NOT_FOUND" in (nonexist_confirm.get("error") or ""),
          str(nonexist_confirm.get("error"))[:120])

    # 校验失败（duration 越界）→ CFG_INVALID 且仍 awaiting_review
    bad_cfg = client.post(f"/api/pipeline/{review_id}/confirm-script", json={
        "script": {"title": "t", "shots": [
            {"shot_id": "shot_001", "prompt": "ok", "action": "", "camera": "",
             "dialogue": "", "duration": 1}]}
    }).json()
    check("confirm-script duration 越界 → CFG_INVALID",
          bad_cfg.get("ok") is False and "CFG_INVALID" in (bad_cfg.get("error") or ""),
          str(bad_cfg.get("error"))[:120])
    still = client.get(f"/api/pipeline/{review_id}/status").json().get("data") or {}
    check("校验失败后仍停留在 awaiting_review",
          still.get("state") == "awaiting_review", f"state={still.get('state')}")

    # 正式确认（无编辑）→ 同 task_id 原地续跑 → director 等待初始图
    confirm = client.post(f"/api/pipeline/{review_id}/confirm-script", json={}).json()
    check("confirm-script 成功返回 state=running",
          confirm.get("ok") is True and (confirm.get("data") or {}).get("state") == "running",
          json.dumps(confirm.get("data"), ensure_ascii=False))
    review_director = _wait_director_running(client, review_id)
    check("确认后 director 进入等待初始图",
          (review_director.get("stages") or {}).get("director", {}).get("state") == "running",
          f"state={review_director.get('state')} error={review_director.get('error')}")
    png2 = b"\x89PNG\r\n\x1a\n" + b"REVIEW_INITIAL_FRAME" * 16
    rv_start = client.post(
        f"/api/pipeline/{review_id}/chain/start",
        files={"file": ("initial.png", png2, "image/png")},
    ).json()
    check("review 任务 chain/start 成功", rv_start.get("ok") is True,
          json.dumps(rv_start.get("data"), ensure_ascii=False))
    rv_seg_count = len(((rv_start.get("data") or {}).get("chain") or {}).get("segments") or [])
    _rv_states, rv_chain_ok = _drive_chain(client, review_id, rv_seg_count)
    check("review 任务逐段确认全部接受", rv_chain_ok, f"states={_rv_states}")

    review_final = _wait_terminal(client, review_id)
    check("确认后同 task_id 续跑至 success",
          review_final.get("state") == "success",
          f"state={review_final.get('state')} error={review_final.get('error')}")
    check("确认续跑后生成 2 个成功镜头",
          len(review_final.get("shots") or []) == 2
          and all(s.get("state") == "success" for s in review_final.get("shots") or []),
          f"shots={[s.get('state') for s in review_final.get('shots') or []]}")

    # 旧路径回归：不传 review 的主任务全程未出现 awaiting_review（director 等待→逐段确认→success）
    check("旧路径（未传 review）：主任务未出现 awaiting_review",
          status.get("state") == "success")

    # ---------- 汇总 ---------- #
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n===== E2E 汇总：{passed}/{total} 通过 =====", flush=True)
    if passed != total:
        print("失败项：", flush=True)
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}: {detail}", flush=True)
    return total - passed


if __name__ == "__main__":
    sys.exit(main())
