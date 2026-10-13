#!/usr/bin/env bash
# 端到端冒烟：前置检查 -> 备份配置 -> 写入桩配置 -> 起桩+服务 -> 跑 e2e_smoke -> 清理 -> 还原配置
#
# 健壮性（P2-6）：
#   * 备份文件名唯一化（含脚本 PID），避免并发/中断把「已是桩配置」的状态当成备份 → 永久污染；
#   * trap 覆盖 EXIT/INT/TERM，且仅清理一次；
#   * 开跑前探测端口是否空闲，占用即明确报错退出（比跑一半才失败更省时间）；
#   * 起服务后自检「本进程存活 + 日志无 bind 失败」，杜绝连到残留旧服务造成「假就绪」；
#   * cleanup 还原后校验 llm_base_url 是否与备份一致，并打印结论。
set -u

# 本机注入的 http_proxy（127.0.0.1:55069）会把「连接 127.0.0.1 → 连接被拒」污染成 HTTP 502，
# 导致 `test-llm 不可达` 一条假失败（55/56）。本脚本只与 127.0.0.1 通信，故自行摘掉代理，
# 保证「直接 bash tests/run_e2e.sh」即可得到真实结果（项目铁律：跑测试必须禁代理）。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy

cd /e/ai/storyforge || exit 1

PY=".venv/Scripts/python.exe"
CFG="config/config.json"
BAK="config/config.json.e2e_bak.$$"
STUB_LOG="logs/_e2e_stub.log"
APP_LOG="logs/_e2e_app.log"
E2E_LOG="logs/_e2e_result.log"
PORT=8123
STUB_PORT=18080
CLEANED=0
BACKUP_DONE=0

mkdir -p logs

# 端口是否已被占用：0=占用，1=空闲
port_in_use() {
  "$PY" - "$1" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.3)
try:
    rc = s.connect_ex(("127.0.0.1", port))
finally:
    s.close()
sys.exit(0 if rc == 0 else 1)
PY
}

cleanup() {
  local rc=$?
  [ "${CLEANED:-0}" = "1" ] && return $rc
  CLEANED=1
  echo "== 清理后台进程 =="
  [ -n "${APP_PID:-}" ] && kill -9 "$APP_PID" 2>/dev/null
  [ -n "${STUB_PID:-}" ] && kill -9 "$STUB_PID" 2>/dev/null
  sleep 1
  echo "== 还原配置 =="
  if [ "${BACKUP_DONE:-0}" = "1" ] && [ -f "$BAK" ]; then
    local want got
    want=$(grep -Eo '"llm_base_url"[^,]*' "$BAK" 2>/dev/null | head -1)
    cp "$BAK" "$CFG"
    got=$(grep -Eo '"llm_base_url"[^,]*' "$CFG" 2>/dev/null | head -1)
    rm -f "$BAK"
    if [ -n "$want" ] && [ "$want" = "$got" ]; then
      echo "已还原 config.json（$got）"
    else
      echo "⚠️ 警告：config.json 还原失败！期望 [$want]，实际 [$got]" >&2
    fi
    # 铁律：config.json 的 md5 不得被 E2E 改动（基线 3a23fc3d09a0c8dce57066d6ec501bfb）
    local md5_after
    md5_after=$("$PY" - "$CFG" <<'PY'
import hashlib, sys
print(hashlib.md5(open(sys.argv[1], "rb").read()).hexdigest())
PY
)
    if [ -n "${BACKUP_MD5:-}" ] && [ "$BACKUP_MD5" = "$md5_after" ]; then
      echo "config.json md5 未变（$md5_after）"
    else
      echo "⚠️ 警告：config.json md5 发生变化！期望 [$BACKUP_MD5]，实际 [$md5_after]" >&2
    fi
  elif [ "${BACKUP_DONE:-0}" = "1" ]; then
    echo "⚠️ 警告：备份 $BAK 丢失，config.json 未还原！" >&2
  else
    echo "（未执行备份，无需还原）"
  fi
  return $rc
}
trap cleanup EXIT INT TERM

echo "== 前置检查：端口是否空闲 =="
for p in "$PORT" "$STUB_PORT"; do
  if port_in_use "$p"; then
    echo "❌ 端口 $p 已被占用，请先杀掉残留进程后再跑" >&2
    echo "   （Win: netstat -ano | findstr :$p  →  taskkill /F /PID <pid>）" >&2
    exit 1
  fi
done
echo "端口 $PORT / $STUB_PORT 均空闲"

echo "== 备份配置 =="
if [ ! -f "$CFG" ]; then
  echo "❌ 未找到 $CFG，无法继续" >&2
  exit 1
fi
cp "$CFG" "$BAK"
BACKUP_DONE=1
BACKUP_MD5=$("$PY" - "$BAK" <<'PY'
import hashlib, sys
print(hashlib.md5(open(sys.argv[1], "rb").read()).hexdigest())
PY
)
echo "备份 -> $BAK（md5=$BACKUP_MD5）"

echo "== 写入桩配置 =="
"$PY" - <<'PY'
import json, pathlib
cfg = {
    "llm_mode": "openai",
    "llm_base_url": "http://127.0.0.1:18080/v1",
    "llm_api_key": "sk-stubtest-abcdefgh1234abcd",
    "llm_model": "stub-model",
    "video_provider": "mock",
    "video_api_key": "",
    "video_base_url": "",
    "video_model": "",
    "resolution": "720p",
    "style": "realistic",
    "shot_count": 3,
    "shot_duration": 2,
    "skip_video_eval": False,
    # RunningHub 工作流 i2v（E2E 走 mock 零成本路径，rh_* 仅显式填默认值，零外网）
    "rh_workflow_id": "2084067455902765057",
    "rh_instance_type": "default",
    "rh_retain_seconds": 180,
    "rh_first_frame_node_id": "4",
    "rh_first_frame_field_name": "image",
    "rh_prompt_node_id": "7",
    "rh_prompt_field_name": "prompt",
    "rh_duration_node_id": "6",
    "rh_duration_field_name": "duration_seconds",
    "rh_seed_node_id": "9",
    "rh_seed_field_name": "seed",
}
pathlib.Path("config/config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print("config written")
PY

echo "== 启动桩 LLM 服务 (:$STUB_PORT) =="
"$PY" -m tests.stub_llm_server "$STUB_PORT" > "$STUB_LOG" 2>&1 &
STUB_PID=$!

echo "== 启动 StoryForge 服务 (:$PORT) =="
STORYFORGE_NO_BROWSER=1 "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --log-level info > "$APP_LOG" 2>&1 &
APP_PID=$!

echo "== 等待服务就绪（并确认本进程存活）=="
READY=0
for i in $(seq 1 60); do
  # 本进程若已退出 → 大概率端口被占用 / bind 失败，立即报错退出，绝不「假就绪」
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "❌ StoryForge 服务（PID=$APP_PID）已退出，疑似端口 $PORT 被占用或启动失败。" >&2
    echo "--- app 日志尾部 ---" >&2
    tail -20 "$APP_LOG" >&2 || true
    exit 1
  fi
  if curl -s "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1; then
    READY=1
    echo "服务已就绪（第 $i 次探测）"
    break
  fi
  sleep 0.5
done

if [ "$READY" -ne 1 ]; then
  echo "❌ 等待超时：StoryForge 服务未就绪（:$PORT）" >&2
  tail -20 "$APP_LOG" >&2 || true
  exit 1
fi

if grep -Eq '10048|Address already in use|error while attempting to bind|WinError 10048' "$APP_LOG" 2>/dev/null; then
  echo "❌ 检测到端口 $PORT 绑定失败（app 日志含 bind 错误），请先杀掉残留进程。" >&2
  exit 1
fi

curl -s "http://127.0.0.1:$PORT/api/health"; echo

echo "== 运行 E2E 冒烟 =="
SF_BASE_URL="http://127.0.0.1:$PORT" \
SF_CONFIG="E:/ai/storyforge/config/config.json" \
SF_EXPECTED_MASK="sk-****abcd" \
"$PY" tests/e2e_smoke.py 2>&1 | tee "$E2E_LOG"
RC=${PIPESTATUS[0]}
echo "E2E exit code = $RC"
echo "== 服务日志尾部 =="
tail -20 "$APP_LOG"
exit $RC
