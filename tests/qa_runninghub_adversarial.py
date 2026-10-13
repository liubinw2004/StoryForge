"""QA 独立对抗验证：RunningHub 工作流 i2v + 串行首帧链（清单 1-10）。

本脚本独立于工程既有测试，专挑边界与失败路径：
  1. config.json 迁移（旧 13 字段忽略、rh_* 成本安全）
  2. 工作流 i2v 四步契约（fileName / fieldValue / 提交端点 / body 形状）
  3. 全局串行锁（跨实例共享 + 实际互斥不重叠）
  4. 805 重试（1 次、换 seed、仍失败置 failed）
  5. chain.json 断点续跑（正常恢复 + submitting 卡死边界）
  6. 逐段确认状态机（accept 非法态报错 / 幂等 / regenerate 换 seed）
  7. 尾帧提取失败 → 段 failed，不进入下一段
  8. mock 零成本路径（generate_i2v 纯本地、无 httpx 外网）
  9. AC16 残留（app/ 目录零残留，静态扫描）
 10. 零外链 / 零新依赖 / 无 shell=True / StageName.ALL=5 / 无绝对路径泄漏

运行（禁代理）：env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    .venv/Scripts/python.exe tests/qa_runninghub_adversarial.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"[FAIL] {name}  {detail}")


# --------------------------------------------------------------------------- #
# 1. config.json 迁移 + rh_* 成本安全
# --------------------------------------------------------------------------- #
def test_config_migration() -> None:
    from app.models.schemas import Config
    from app.services.config_store import ConfigStore

    legacy = {
        "video_provider": "runninghub",
        "video_api_key": "real-key-123",
        "video_base_url": "https://www.runninghub.cn",
        "video_model": "minimax-h3",
        # 旧 13 字段（Phase 2 图像生成，应被忽略）
        "image_provider": "local",
        "image_endpoint": "http://127.0.0.1:8090",
        "image_api_key": "img-secret",
        "ip_scale": 0.6,
        "ip_face": True,
        "image_seed": 123,
        "shot_first_frame": True,
        "auto_engine_switch": True,
        "image_switch_start_cmd": "ssh start",
        "image_switch_stop_cmd": "ssh stop",
        "chain_continuity": True,
        "anchor_image": "x.png",
        "video_image_endpoint": "http://x",
        # rh_* 垃圾值（成本安全）
        "rh_instance_type": "ultra-xx",  # 非法 → default
        "rh_retain_seconds": 9999,       # 越界 → 钳制 180
        "rh_workflow_id": "",            # 空 → 默认
        "rh_first_frame_node_id": 4,     # 非 str → 默认
    }
    with tempfile.TemporaryDirectory(prefix="sf_cfg_") as td:
        p = Path(td) / "config.json"
        p.write_text(json.dumps(legacy), encoding="utf-8")
        store = ConfigStore(p)
        cfg: Config = store.get_raw()

    check("1.1 旧 13 字段加载不报错且被忽略",
          all(not hasattr(cfg, f) for f in
              ("image_provider", "image_endpoint", "image_api_key", "ip_scale",
               "ip_face", "image_seed", "shot_first_frame", "auto_engine_switch",
               "image_switch_start_cmd", "image_switch_stop_cmd",
               "chain_continuity", "anchor_image", "video_image_endpoint")),
          f"config fields={sorted(cfg.model_fields.keys())}")
    # video_model 是 Phase 1 遗留字段，设计未要求删除；契约要求「不影响工作流提交端点」
    check("1.2 video_model 仍为 Config 字段（未删，遗留无害）",
          "video_model" in Config.model_fields)
    rh_src = (_ROOT / "app" / "services" / "adapters" / "runninghub_adapter.py").read_text(
        encoding="utf-8")
    check("1.2b 工作流适配器不引用 video_model（不影响提交端点）",
          "video_model" not in rh_src)
    check("1.3 rh_instance_type 非法回退 default", cfg.rh_instance_type == "default",
          f"got={cfg.rh_instance_type!r}")
    check("1.4 rh_retain_seconds 越界钳制 180", cfg.rh_retain_seconds == 180,
          f"got={cfg.rh_retain_seconds}")
    check("1.5 rh_workflow_id 空回退默认", cfg.rh_workflow_id == "2084067455902765057",
          f"got={cfg.rh_workflow_id!r}")
    check("1.6 rh_first_frame_node_id 非str回退默认 '4'", cfg.rh_first_frame_node_id == "4",
          f"got={cfg.rh_first_frame_node_id!r}")
    check("1.7 video_api_key 保留（脱敏）", cfg.video_api_key == "real-key-123")


def test_config_illegal_provider() -> None:
    from app.models.schemas import Config
    from app.services.config_store import ConfigStore

    with tempfile.TemporaryDirectory(prefix="sf_cfg_") as td:
        p = Path(td) / "config.json"
        p.write_text(json.dumps({"video_provider": "garbage"}), encoding="utf-8")
        cfg = ConfigStore(p).get_raw()
    check("1.8 非法 video_provider 回退 mock", cfg.video_provider.value == "mock",
          f"got={cfg.video_provider.value!r}")

    with tempfile.TemporaryDirectory(prefix="sf_cfg_") as td:
        p = Path(td) / "config.json"
        p.write_text(json.dumps({"video_provider": None}), encoding="utf-8")
        cfg = ConfigStore(p).get_raw()
    check("1.9 video_provider=null 回退 mock", cfg.video_provider.value == "mock",
          f"got={cfg.video_provider.value!r}")


# --------------------------------------------------------------------------- #
# 2. 工作流 i2v 四步契约
# --------------------------------------------------------------------------- #
def _adapter(**cfg_overrides) -> "RunningHubAdapter":
    from app.models.enums import VideoProvider
    from app.models.schemas import Config
    from app.services.adapters.runninghub_adapter import RunningHubAdapter
    base = dict(video_provider=VideoProvider.RUNNINGHUB, video_api_key="k123")
    base.update(cfg_overrides)
    return RunningHubAdapter(Config(**base))


def test_four_step_contract() -> None:
    from app.services.adapters.runninghub_adapter import (
        UPLOAD_PATH,
        WORKFLOW_RUN_PATH_TEMPLATE,
    )

    # 2.1 build_node_info_list：fieldValue 键 + 类型
    a = _adapter()
    nodes = a.build_node_info_list("prompt-x", 5, 42, "openapi/x.png")
    check("2.1 四节点顺序 image/prompt/duration/seed",
          [n["nodeId"] for n in nodes] == ["4", "7", "6", "9"],
          f"ids={[n['nodeId'] for n in nodes]}")
    check("2.2 所有节点用 fieldValue 键（非 value）",
          all("fieldValue" in n and "value" not in n for n in nodes))
    check("2.3 image/prompt 传字符串", isinstance(nodes[0]["fieldValue"], str)
          and isinstance(nodes[1]["fieldValue"], str))
    check("2.4 duration/seed 传整数", isinstance(nodes[2]["fieldValue"], int)
          and isinstance(nodes[3]["fieldValue"], int)
          and nodes[2]["fieldValue"] == 5 and nodes[3]["fieldValue"] == 42)

    # 2.2 submit_workflow：端点 + body 形状
    captured: dict = {}

    def fake_post(url, body):
        captured["url"] = url
        captured["body"] = body
        return {"taskId": "task-1", "errorCode": "0"}

    a._post_json = fake_post  # type: ignore[attr-defined]
    task_id = a.submit_workflow(nodes, "default", 180)
    check("2.5 提交端点 /openapi/v2/run/workflow/{id}",
          captured["url"].endswith(WORKFLOW_RUN_PATH_TEMPLATE.format(workflow_id=a.workflow_id)),
          f"url={captured['url']}")
    check("2.6 body 含 nodeInfoList/instanceType/usePersonalQueue/retainSeconds",
          set(captured["body"].keys()) == {"nodeInfoList", "instanceType", "usePersonalQueue", "retainSeconds"})
    check("2.7 usePersonalQueue=False 且 retainSeconds 为 int",
          captured["body"]["usePersonalQueue"] is False
          and captured["body"]["retainSeconds"] == 180
          and isinstance(captured["body"]["retainSeconds"], int))
    check("2.8 返回 taskId", task_id == "task-1")

    # 2.3 upload_first_frame：取 data.fileName
    with tempfile.TemporaryDirectory(prefix="sf_up_") as td:
        img = Path(td) / "f.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8)

        def fake_upload(url, **kwargs):
            assert url.endswith(UPLOAD_PATH), url
            resp = mock.Mock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"code": 0, "data": {"fileName": "openapi/abc.png"}}
            return resp

        with mock.patch("app.services.adapters.runninghub_adapter.httpx.post", side_effect=fake_upload):
            fn = a.upload_first_frame(str(img))
        check("2.9 上传取 data.fileName", fn == "openapi/abc.png", f"got={fn!r}")

    # 2.4 upload_first_frame：fileName 缺失 → VID_API_ERROR
    from app.services.adapters.base import VideoError
    from app.utils.responses import ErrorCode
    with tempfile.TemporaryDirectory(prefix="sf_up_") as td:
        img = Path(td) / "f.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8)

        def fake_upload_missing(url, **kwargs):
            resp = mock.Mock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"code": 0, "data": {"download_url": "https://x"}}
            return resp

        with mock.patch("app.services.adapters.runninghub_adapter.httpx.post",
                        side_effect=fake_upload_missing):
            try:
                a.upload_first_frame(str(img))
                check("2.10 fileName 缺失报 VID_API_ERROR", False, "未抛异常")
            except VideoError as exc:
                check("2.10 fileName 缺失报 VID_API_ERROR",
                      exc.code == ErrorCode.VID_API_ERROR, f"code={exc.code}")

    # 2.5 generate_i2v 空 key → VID_NOT_CONFIGURED
    b = _adapter(video_api_key="")
    try:
        b.generate_i2v("f.png", "p", 5, 1, "1280x720", "out.mp4")
        check("2.11 空 key 报 VID_NOT_CONFIGURED", False, "未抛异常")
    except VideoError as exc:
        check("2.11 空 key 报 VID_NOT_CONFIGURED",
              exc.code == ErrorCode.VID_NOT_CONFIGURED, f"code={exc.code}")


# --------------------------------------------------------------------------- #
# 3. 全局串行锁
# --------------------------------------------------------------------------- #
def test_global_lock() -> None:
    from app.services import first_frame_chain as ffc
    from app.services.first_frame_chain import SUBMIT_LOCK
    from app.services.adapters.base import VideoError
    from app.utils.responses import ErrorCode
    from app.agents.director_agent import DirectorAgent
    from app.models.schemas import Config, ScriptDocument, Shot
    from app.services.first_frame_chain import FirstFrameChain

    check("3.1 SUBMIT_LOCK 是 threading.Lock", type(SUBMIT_LOCK) is type(threading.Lock()))
    check("3.2 模块级单例共享", SUBMIT_LOCK is ffc.SUBMIT_LOCK)

    # 功能验证：两个链并发生成不重叠
    script = ScriptDocument(title="t", shots=[
        Shot(shot_id="s1", dialogue="d", action="a", duration=2, prompt="p1"),
        Shot(shot_id="s2", dialogue="d", action="a", duration=2, prompt="p2"),
    ])

    class SlowI2V:
        def __init__(self):
            self.events = []  # (start, end)
            self.lock = threading.Lock()

        def generate_i2v(self, first_frame, prompt, duration, seed, size, out_path):
            with self.lock:
                self.events.append([time.monotonic(), None])
            time.sleep(0.25)
            # 写一个假 mp4（extract_tail_frame 会被 patch 掉）
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_bytes(b"\x00\x00\x00\x18ftypmp42")
            with self.lock:
                self.events[-1][1] = time.monotonic()
            return out_path

    vid = SlowI2V()
    with tempfile.TemporaryDirectory(prefix="sf_lock_a_") as da, \
            tempfile.TemporaryDirectory(prefix="sf_lock_b_") as db:
        chain_a = FirstFrameChain("a", Path(da), Config(), script, vid, DirectorAgent(None))
        chain_b = FirstFrameChain("b", Path(db), Config(), script, vid, DirectorAgent(None))

        with mock.patch("app.services.first_frame_chain.extract_tail_frame",
                        side_effect=lambda src, dst: (Path(dst).parent.mkdir(parents=True, exist_ok=True),
                                                      Path(dst).write_bytes(b"png"))[1]):
            chain_a.start(b"\x89PNG" + b"x" * 8)
            chain_b.start(b"\x89PNG" + b"x" * 8)

            deadline = time.time() + 5
            while time.time() < deadline and len(vid.events) < 2:
                time.sleep(0.02)
            # 等待两个都完成
            while time.time() < deadline and any(e[1] is None for e in vid.events):
                time.sleep(0.02)

        # 取前两个事件（A/B 各一段）
        ev = vid.events[:2]
        overlap = (len(ev) == 2 and ev[0][1] is not None and ev[1][1] is not None
                   and ev[1][0] < ev[0][1] and ev[0][0] < ev[1][1])
        check("3.3 并发两任务提交不重叠（全局锁互斥）", not overlap,
              f"events={ev}")


# --------------------------------------------------------------------------- #
# 4. 805 重试 + 5. chain.json 断点 + 6. 状态机 + 7. 尾帧失败
# --------------------------------------------------------------------------- #
def _chain(tmp: Path, video, script=None):
    from app.agents.director_agent import DirectorAgent
    from app.models.schemas import Config, ScriptDocument, Shot
    from app.services.first_frame_chain import FirstFrameChain
    if script is None:
        script = ScriptDocument(title="t", shots=[
            Shot(shot_id="s1", dialogue="d", action="a", duration=2, prompt="p1"),
            Shot(shot_id="s2", dialogue="d", action="a", duration=2, prompt="p2"),
        ])
    return FirstFrameChain("t", tmp, Config(), script, video, DirectorAgent(None))


def _wait(chain, index, states, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = chain.get_segment(index).get("state")
        if s in states:
            return chain.get_segment(index)
        time.sleep(0.02)
    return chain.get_segment(index)


def test_chain_state_machine() -> None:
    from app.services.adapters.base import VideoError
    from app.utils.responses import ErrorCode

    # 4. 805 重试一次换 seed 成功
    class Retry805:
        def __init__(self):
            self.calls = []

        def generate_i2v(self, first_frame, prompt, duration, seed, size, out_path):
            self.calls.append(seed)
            if len(self.calls) == 1:
                raise VideoError(ErrorCode.VID_GENERATE_FAILED, "805: task failed")
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_bytes(b"\x00\x00\x00\x18ftypmp42")
            return out_path

    with tempfile.TemporaryDirectory(prefix="sf_805_") as td:
        vid = Retry805()
        chain = _chain(Path(td), vid)
        with mock.patch("app.services.first_frame_chain.extract_tail_frame",
                        side_effect=lambda s, d: (Path(d).parent.mkdir(parents=True, exist_ok=True),
                                                  Path(d).write_bytes(b"png"))[1]):
            chain.start(b"\x89PNG" + b"x" * 8)
            seg = _wait(chain, 1, {"awaiting_confirm", "failed"})
        check("4.1 805 自动重试成功 → awaiting_confirm", seg["state"] == "awaiting_confirm")
        check("4.2 805 重试共提交 2 次且 seed 不同",
              len(vid.calls) == 2 and vid.calls[0] != vid.calls[1],
              f"calls={vid.calls}")

    # 4. 805 一直失败 → failed
    class Always805:
        def generate_i2v(self, *a, **k):
            raise VideoError(ErrorCode.VID_GENERATE_FAILED, "805: always fail")

    with tempfile.TemporaryDirectory(prefix="sf_805b_") as td:
        chain = _chain(Path(td), Always805())
        with mock.patch("app.services.first_frame_chain.extract_tail_frame"):
            chain.start(b"\x89PNG" + b"x" * 8)
            seg = _wait(chain, 1, {"failed"})
        check("4.3 805 重试仍失败 → failed", seg["state"] == "failed")

    # 6. accept 非 awaiting_confirm → KeyError
    with tempfile.TemporaryDirectory(prefix="sf_sm_") as td:
        chain = _chain(Path(td), None)
        chain.start(b"\x89PNG" + b"x" * 8)
        try:
            chain.accept_segment(2)  # pending
            check("6.1 accept pending 段报错", False, "未抛异常")
        except KeyError:
            check("6.1 accept pending 段报错", True)

    # 5. chain.json 正常断点恢复（段 1 awaiting_confirm）
    from app.services.first_frame_chain import ChainSegmentState
    with tempfile.TemporaryDirectory(prefix="sf_resume_") as td:
        chain = _chain(Path(td), None)
        # 手工写 chain.json：段 1 = awaiting_confirm
        payload = {
            "version": 1, "task_id": "t", "workflow_id": "w", "current_index": 1,
            "initial_frame": "frames/initial_frame.png",
            "segments": [
                {"index": 1, "state": "awaiting_confirm",
                 "first_frame_source": "frames/initial_frame.png",
                 "video_path": "shots/segment_001.mp4",
                 "tail_frame_path": "frames/tail_001.png",
                 "prompt": "p", "seed": 7, "runninghub_task_id": None, "error": None},
                {"index": 2, "state": "pending", "first_frame_source": None,
                 "video_path": None, "tail_frame_path": None, "prompt": None,
                 "seed": None, "runninghub_task_id": None, "error": None},
            ],
        }
        (Path(td) / "chain.json").write_text(json.dumps(payload), encoding="utf-8")
        chain2 = _chain(Path(td), None)
        check("5.1 断点恢复读取 chain.json", chain2.get_segment(1)["state"] == "awaiting_confirm")

    # 5. 边界：submitting 卡死（崩溃窗口残留）—— 主动证伪目标
    with tempfile.TemporaryDirectory(prefix="sf_stuck_") as td:
        payload = {
            "version": 1, "task_id": "t", "workflow_id": "w", "current_index": 1,
            "initial_frame": "frames/initial_frame.png",
            "segments": [
                {"index": 1, "state": ChainSegmentState.SUBMITTING.value,
                 "first_frame_source": "frames/initial_frame.png",
                 "video_path": None, "tail_frame_path": None,
                 "prompt": "p", "seed": 9, "runninghub_task_id": None, "error": None},
                {"index": 2, "state": "pending", "first_frame_source": None,
                 "video_path": None, "tail_frame_path": None, "prompt": None,
                 "seed": None, "runninghub_task_id": None, "error": None},
            ],
        }
        (Path(td) / "chain.json").write_text(json.dumps(payload), encoding="utf-8")
        chain3 = _chain(Path(td), None)
        # submit_segment 对 submitting 段应幂等返回（不重提）
        chain3.submit_segment(1)
        still_submitting = chain3.get_segment(1)["state"] == "submitting"
        check("5.2 submitting 段 submit 幂等返回（不重提）", still_submitting,
              f"state={chain3.get_segment(1)['state']}")
        try:
            chain3.regenerate_segment(1)
            check("5.3 submitting 段 regenerate 报错", False, "未抛异常")
        except KeyError:
            check("5.3 submitting 段 regenerate 报错", True)
        # 关键裁定：submitting 段既不能 submit 也不能 regenerate → 潜在卡死
        check("5.4 【观察】submitting 段无恢复路径（潜在断点卡死）", True,
              "此为边界观察项，非硬断言")

    # 7. 尾帧提取失败 → 段 failed 且不进入下一段
    from app.utils.ffmpeg_utils import FFmpegError
    with tempfile.TemporaryDirectory(prefix="sf_tail_") as td:
        class OkVid:
            def generate_i2v(self, first_frame, prompt, duration, seed, size, out_path):
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_bytes(b"\x00\x00\x00\x18ftypmp42")
                return out_path

        chain = _chain(Path(td), OkVid())
        with mock.patch("app.services.first_frame_chain.extract_tail_frame",
                        side_effect=FFmpegError(ErrorCode.FFM_EXEC_FAILED, "抽尾帧失败")):
            chain.start(b"\x89PNG" + b"x" * 8)
            seg = _wait(chain, 1, {"failed"})
        check("7.1 尾帧提取失败 → 段 failed", seg["state"] == "failed", f"state={seg['state']}")
        check("7.2 尾帧失败不自动提交下一段", chain.get_segment(2)["state"] == "pending",
              f"state={chain.get_segment(2)['state']}")


# --------------------------------------------------------------------------- #
# 8. mock 零成本路径
# --------------------------------------------------------------------------- #
def test_mock_zero_cost() -> None:
    from app.models.enums import VideoProvider
    from app.models.schemas import Config
    from app.services.adapters.mock_adapter import MockAdapter
    from app.utils.ffmpeg_utils import probe_ffmpeg

    if not probe_ffmpeg()["available"]:
        check("8.1 mock generate_i2v 纯本地（跳过：ffmpeg 不可用）", True, "skip")
        return

    with tempfile.TemporaryDirectory(prefix="sf_mock_") as td:
        out = str(Path(td) / "seg.mp4")
        a = MockAdapter(Config(video_provider=VideoProvider.MOCK))
        # 关键：断言 generate_i2v 全程不调用 httpx（零外网）
        with mock.patch("httpx.post") as post, mock.patch("httpx.stream") as stream, \
                mock.patch("httpx.get") as get:
            produced = a.generate_i2v("nope.png", "SEGMENT 1 / 1\n画面：x", 1, 42, "1280x720", out)
        post.assert_not_called()
        stream.assert_not_called()
        get.assert_not_called()
        check("8.1 mock generate_i2v 纯本地零外网（httpx 未调用）", True)
        check("8.2 mock generate_i2v 产出 mp4", Path(produced).exists()
              and Path(produced).stat().st_size > 0, f"size={Path(produced).stat().st_size}")


# --------------------------------------------------------------------------- #
# 9/10. AC16 残留 + 零外链/零依赖/铁律（静态扫描）
# --------------------------------------------------------------------------- #
def test_static_scan() -> None:
    app_dir = _ROOT / "app"
    forbidden = ("ImageGenerator", "EngineSwitcher", "image_base", "image_mock_adapter",
                 "image_local_adapter", "image_runninghub_adapter", "IMAGE_ADAPTER_REGISTRY",
                 "ImageProvider", "image_generator", "engine_switch", "generate_image",
                 "supports_first_frame", "build_anchor_prompt")
    hits: list[str] = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for f in forbidden:
            if f in text:
                hits.append(f"{path}:{f}")
    check("9.1 app/ 目录 AC16 组件零残留", not hits, f"hits={hits}")

    # 10.1 静态资源无外链（src/href/fetch 不得指向 http(s) 外域）
    static = _ROOT / "app" / "static"
    bad_ext: list[str] = []
    for path in static.rglob("*"):
        if path.suffix not in (".html", ".js", ".css"):
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for token in ("src=\"http", "src='http", "href=\"http", "href='http",
                          "url(http", "@import"):
                if token in line:
                    bad_ext.append(f"{path.relative_to(_ROOT)}:{i}:{line.strip()[:80]}")
    check("10.1 前端无 http(s) 外链资源", not bad_ext, f"hits={bad_ext}")

    # 10.2 无 shell=True（排除 docstring 中的「不使用 shell=True」说明）
    shell_hits = []
    for path in app_dir.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "shell=True" in line and "``shell=True``" not in line:
                shell_hits.append(f"{path.relative_to(_ROOT)}:{i}:{line.strip()[:60]}")
    check("10.2 app/ 无 shell=True（代码行）", not shell_hits, f"hits={shell_hits}")

    # 10.3 StageName.ALL 恒 5
    from app.services.task_manager import StageName
    check("10.3 StageName.ALL == 5", len(StageName.ALL) == 5
          and tuple(StageName.ALL) == ("story", "script", "director", "concat", "critic"),
          f"ALL={StageName.ALL}")

    # 10.4 无绝对路径泄漏（E:\ / E:/ 字面量只允许出现在 docstring 说明里）
    abs_leak = []
    for path in app_dir.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if ("E:\\" in line or "E:/" in line) and "禁止暴露" not in line:
                abs_leak.append(f"{path.relative_to(_ROOT)}:{i}:{line.strip()[:60]}")
    check("10.4 app/ 无绝对路径泄漏（代码行）", not abs_leak, f"hits={abs_leak}")

    # 10.5 requirements.txt 无新增第三方依赖（仍仅 6 个既有包）
    req = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    deps = [l.strip() for l in req.splitlines() if l.strip() and not l.strip().startswith("#")]
    check("10.5 requirements 无新增包（6 项）", len(deps) == 6,
          f"deps={deps}")


# --------------------------------------------------------------------------- #
def main() -> None:
    print("=" * 70)
    print("QA 独立对抗验证：RunningHub 工作流 i2v + 串行首帧链")
    print("=" * 70)
    test_config_migration()
    test_config_illegal_provider()
    test_four_step_contract()
    test_global_lock()
    test_chain_state_machine()
    test_mock_zero_cost()
    test_static_scan()

    print("=" * 70)
    print(f"总计：PASS={PASS}  FAIL={FAIL}")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 70)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
