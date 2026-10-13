# -*- coding: utf-8 -*-
"""
StoryForge → 远程 llama.cpp LLM 稳定性压测脚本

用法：
    python scripts/stability_test.py [轮次]        # 默认 8 轮
前提：
    已建立 SSH 隧道  ssh -N -L 8080:127.0.0.1:8080 -p 29902 ubuntu@hpc.vmip.com.cn

输出：
    控制台实时进度 + logs/stability_results.json（原始数据）
"""
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080/v1"
CHAT = BASE + "/chat/completions"
MODEL = "qwen3-30b-a3b"
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 8

PROMPTS = [
    ("短", "你好", 32),
    ("短", "用一句话说明什么是分镜脚本", 64),
    ("中", "把这段对话转成分镜脚本JSON：小明和小红在咖啡馆讨论要不要一起创业。只输出JSON。", 512),
    ("中", "为一段30秒短剧写3个镜头的详细描述，包含机位、动作与对白。", 512),
    ("长",
     "你是编剧。请把下面剧情拆成6个镜头的分镜脚本，只输出JSON，字段含 "
     "title/characters/scene/positions/shots（每个shot含 shot_id/dialogue/action/camera/duration/prompt）。"
     "剧情：一个外卖员在雨夜送餐时救了一只流浪猫，第二天发现猫的主人竟是他失联多年的妹妹。",
     2048),
]

JSON_KEYS = ("title", "characters", "scene", "positions", "shots")


def call(messages, max_tokens, timeout=900, model=MODEL, raw=None):
    payload = raw if raw is not None else {
        "model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.6,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CHAT, data=data, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        dt = time.time() - t0
        ch = (body.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        content = msg.get("content") or ""
        usage = body.get("usage") or {}
        ct = usage.get("completion_tokens", 0) or 0
        return dict(ok=True, http=200, latency=dt, content=content,
                    reasoning_none=msg.get("reasoning_content") is None,
                    finish=ch.get("finish_reason"), completion_tokens=ct,
                    tps=(ct / (dt - (usage.get("prompt_tokens", 0) and 0)) if dt > 0 else 0),
                    usage=usage, err=None)
    except urllib.error.HTTPError as e:
        return dict(ok=False, http=e.code, latency=time.time() - t0, content="",
                    err=e.read().decode("utf-8", "replace")[:200], reasoning_none=None,
                    finish=None, completion_tokens=0, tps=0, usage={})
    except Exception as e:
        return dict(ok=False, http=None, latency=time.time() - t0, content="",
                    err=f"{type(e).__name__}: {e}", reasoning_none=None,
                    finish=None, completion_tokens=0, tps=0, usage={})


def is_valid_script(content):
    try:
        obj = json.loads(content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except Exception:
        return False, "JSON 解析失败"
    missing = [k for k in JSON_KEYS if k not in obj]
    if missing:
        return False, f"缺字段 {missing}"
    n = len(obj.get("shots") or [])
    if n == 0:
        return False, "shots 为空"
    return True, f"合法 JSON，{n} 个镜头"


def main():
    print(f"目标: {CHAT}")
    print(f"模型: {MODEL} | 轮次: {ROUNDS} | 每轮 {len(PROMPTS)} 条 = {ROUNDS * len(PROMPTS)} 次请求\n")
    recs = []

    # ---------- 预热 ----------
    print("[预热] 第一次调用…", flush=True)
    r = call([{"role": "user", "content": "你好"}], 16)
    print(f"  预热: ok={r['ok']} 耗时={r['latency']:.2f}s\n", flush=True)

    # ---------- Phase 1: 顺序混合负载 ----------
    print("=" * 72)
    print("Phase 1 · 顺序混合负载")
    print("=" * 72)
    for rd in range(1, ROUNDS + 1):
        line = []
        for tag, prompt, mt in PROMPTS:
            r = call([{"role": "user", "content": prompt}], mt)
            r.update(phase="seq", round=rd, tag=tag, mt=mt)
            r["content_len"] = len(r["content"])
            recs.append(r)
            mark = "OK " if (r["ok"] and r["content"]) else "FAIL"
            line.append(f"{tag}{mt}:{mark}{r['latency']:.1f}s")
        print(f"  轮 {rd:2d}/{ROUNDS} | " + " | ".join(line), flush=True)

    # ---------- Phase 2: 长输出 / JSON 结构 ----------
    print("\n" + "=" * 72)
    print("Phase 2 · 长输出与 JSON 结构校验（5 次）")
    print("=" * 72)
    for i in range(1, 6):
        r = call([{"role": "user", "content": PROMPTS[4][1]}], 2048)
        ok_json, why = is_valid_script(r["content"]) if r["ok"] else (False, r["err"])
        r.update(phase="json", round=i, tag="json", mt=2048, content_len=len(r["content"]))
        r["json_ok"] = ok_json
        r["json_why"] = why
        recs.append(r)
        print(f"  第{i}次: ok={r['ok']} len={r['content_len']} finish={r['finish']} "
              f"耗时={r['latency']:.1f}s → {why}", flush=True)

    # ---------- Phase 3: 错误处理 ----------
    print("\n" + "=" * 72)
    print("Phase 3 · 错误处理（服务不应崩溃）")
    print("=" * 72)
    err_cases = [
        ("不存在的模型", None, {"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}),
        ("空 messages", [{"role": "user", "content": ""}], None),
        ("非法 JSON 体", None, "BROKEN"),
    ]
    for name, msgs, raw in err_cases:
        if raw == "BROKEN":
            try:
                req = urllib.request.Request(CHAT, data=b"{not json",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    code, err = resp.status, None
            except urllib.error.HTTPError as e:
                code, err = e.code, e.read().decode("utf-8", "replace")[:120]
            except Exception as e:
                code, err = None, str(e)[:120]
            print(f"  {name}: HTTP {code} | {err}")
            recs.append(dict(phase="err", tag=name, ok=False, http=code, err=err,
                             latency=0, content="", content_len=0))
        else:
            r = call(msgs, 8, raw=raw)
            r.update(phase="err", tag=name, content_len=len(r["content"]))
            recs.append(r)
            print(f"  {name}: HTTP {r['http']} ok={r['ok']} | {(r['err'] or '')[:100]}", flush=True)

    # 错误后服务是否仍正常
    r = call([{"role": "user", "content": "恢复检查：说“正常”"}], 16)
    print(f"  → 错误后恢复检查: ok={r['ok']} content={r['content'][:30]!r}", flush=True)

    # ---------- Phase 4: 并发 ----------
    print("\n" + "=" * 72)
    print("Phase 4 · 并发 2 请求（验证 并发=1 限制）")
    print("=" * 72)
    out = {}

    def worker(i):
        out[i] = call([{"role": "user", "content": f"并发测试{i}：写一句关于雨天的短句"}], 64)

    ths = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.time() - t0
    for i in (1, 2):
        r = out[i]
        recs.append(dict(r, phase="concurrent", tag=f"并发{i}", content_len=len(r["content"])))
        print(f"  并发{i}: ok={r['ok']} http={r['http']} 耗时={r['latency']:.1f}s "
              f"len={len(r['content'])} | {(r['err'] or '')[:80]}", flush=True)
    print(f"  两个并发请求墙钟合计: {wall:.1f}s（若≈两者耗时之和则为串行排队）", flush=True)

    # ---------- 汇总 ----------
    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    seq = [r for r in recs if r.get("phase") == "seq"]
    ok_seq = [r for r in seq if r["ok"] and r["content"]]
    lat = sorted(r["latency"] for r in ok_seq)

    def pct(p):
        return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0

    summary = {
        "总请求数": len([r for r in recs if r.get("phase") in ("seq", "json")]),
        "成功(HTTP200且有正文)": len(ok_seq),
        "成功率": f"{len(ok_seq) / max(1, len(seq)) * 100:.1f}%",
        "正文为空次数": sum(1 for r in seq if r["ok"] and not r["content"]),
        "reasoning 泄漏次数": sum(1 for r in seq if r.get("reasoning_none") is False),
        "延迟_P50": f"{pct(0.5):.2f}s",
        "延迟_P95": f"{pct(0.95):.2f}s",
        "延迟_最大": f"{lat[-1]:.2f}s" if lat else "-",
        "延迟_最小": f"{lat[0]:.2f}s" if lat else "-",
        "平均生成速度": f"{statistics.mean([r['tps'] for r in ok_seq]):.1f} tok/s" if ok_seq else "-",
        "JSON结构校验通过": f"{sum(1 for r in recs if r.get('json_ok'))}/5",
    }
    for k, v in summary.items():
        print(f"  {k:22s}: {v}")

    os.makedirs("logs", exist_ok=True)
    with open("logs/stability_results.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "records": recs}, f, ensure_ascii=False, indent=1)
    print("\n原始数据已写入 logs/stability_results.json")


if __name__ == "__main__":
    main()
