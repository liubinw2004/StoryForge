# 测试报告：RunningHub 工作流 i2v + 串行首帧链

> 文档类型：**QA 独立对抗验证报告**
> 项目：StoryForge（Python 3.13 + FastAPI + 原生 HTML/CSS/JS）
> 版本：v0.4 增量　|　语言：简体中文　|　作者：Edward（QA Engineer）
> 验证对象：工程师交付的「RunningHub 工作流 i2v + 串行首帧链」实现
> 输入：`docs/增量设计-RunningHub工作流首帧链.md`、`docs/增量PRD-RunningHub工作流首帧链.md`、`docs/RunningHub工作流API接入方案.md`

---

## 一、结论（Routing Decision）

**Routing = NoOne（全通过，放行）**

工程师自称 `IS_PASS YES（384 单测 + 63 E2E 全离线）`。经独立复现与对抗证伪：

| 验证项 | 工程师声称 | QA 独立复现 | 判定 |
|---|---|---|---|
| 全量单测 | 384 OK | **384 OK**（93s，禁代理） | ✅ 一致 |
| E2E | 63 全离线 | **63/63 通过**（mock 全离线，禁代理） | ✅ 一致 |
| 专项对抗 | — | **42/42 断言通过** | ✅ 主动证伪未发现源码缺陷 |

全部 18 条验收标准（AC1–AC18）在正常与边界路径下均满足。主动证伪发现的唯一边界观察（§五 G1：`submitting` 状态无自动恢复路径）为**崩溃窗口内的极端边界**，不构成阻断性 bug，归入「已知观察项」记录，不下发修复。

---

## 二、验证环境与方法（铁律合规）

| 铁律 | 执行 |
|---|---|
| 测试禁代理 | ✅ 全量单测 / E2E / 对抗脚本均 `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy` |
| E2E 不触网 | ✅ `run_e2e.sh` 用 stub LLM 服务 + mock 适配器，全程零外网；RunningHub 四步用 `httpx.MockTransport`/mock.patch |
| 不动 `config/config.json` | ✅ E2E 临时改写后还原，`md5` 前后一致（`6d70f9ee075c2e3d68fd2bc916ce9909`） |

### 复现命令
```bash
# 全量单测（禁代理）
cd /e/ai/storyforge && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u ALL_PROXY -u all_proxy .venv/Scripts/python.exe -m unittest discover -s tests -t .
# → Ran 384 tests in 93.085s  OK

# E2E（mock 全离线）
bash tests/run_e2e.sh
# → 63/63 通过，E2E exit code = 0，config.json md5 未变

# QA 独立对抗脚本（本次新增）
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u ALL_PROXY -u all_proxy .venv/Scripts/python.exe tests/qa_runninghub_adversarial.py
# → 总计 PASS=42 FAIL=0

# 前端语法
"C:/Program Files/nodejs/node.exe" --check "E:/ai/storyforge/app/static/app.js"
# → exit 0
```

---

## 三、重点清单逐项核验（10 条）

### 1. config.json 迁移 + `rh_*` 成本安全　✅
- **旧 13 字段忽略不报错**：写入含 `image_provider/image_endpoint/image_api_key/ip_scale/ip_face/image_seed/shot_first_frame/auto_engine_switch/image_switch_start_cmd/image_switch_stop_cmd/chain_continuity/anchor_image/video_image_endpoint` 的旧 JSON，`ConfigStore` 加载成功，13 字段全部被 `Config(extra="ignore")` 丢弃，不触发整份配置重置。
- **`rh_*` 成本安全**：`rh_instance_type="ultra-xx"` → 回退 `default`；`rh_retain_seconds=9999` → 钳制 `180`；`rh_workflow_id=""` → 回退默认 `2084067455902765057`；`rh_first_frame_node_id=4`（非 str）→ 回退 `"4"`；`video_api_key` 保留真实值并对外脱敏。
- **旧 `video_base_url`/`video_model` 不影响工作流端点**：`video_model` 为 Phase 1 遗留字段（设计未要求删除，保留无害），`RunningHubAdapter` 源码**零引用** `video_model`；提交端点由 `rh_workflow_id` 决定（`/openapi/v2/run/workflow/{rh_workflow_id}`），`video_base_url` 仅作为 host 来源，符合设计。
- **非法 provider 回退**：`video_provider="garbage"` 与 `video_provider=null` 均回退 `mock`，不崩溃。

### 2. 工作流 i2v 四步契约　✅
- **`upload_first_frame`**：取 `data.fileName`（非 `download_url`）；`fileName` 缺失/为空 → `VID_API_ERROR`（AC10/AC15）。
- **`build_node_info_list`**：四节点顺序 `image(4)/prompt(7)/duration(6)/seed(9)`；字段名恒用 **`fieldValue`**（非 `value`）；image/prompt 传**字符串**、duration/seed 传**整数**（对齐 API 方案 §5.3）。
- **`submit_workflow`**：端点 `/openapi/v2/run/workflow/{id}`；body 精确含 `nodeInfoList/instanceType/usePersonalQueue/retainSeconds`，`usePersonalQueue=False`、`retainSeconds` 为 int。
- **`generate_i2v`** 空 `video_api_key` → `VID_NOT_CONFIGURED`（AC15，不静默、不降级 mock）。

### 3. 全局串行锁（防 421）　✅
- `SUBMIT_LOCK = threading.Lock()`，模块级单例，跨任务共享。
- **功能证伪**：两个不同 `FirstFrameChain` 并发生成，`generate_i2v` 调用时间窗口**不重叠**（第二个在第一个释放锁后才进入），证明确实互斥、同一时刻仅 1 个 RunningHub 提交在飞。

### 4. 805 自动重试　✅
- 命中 805 → 自动换随机 seed **重试 1 次**（共 2 次提交，两次 seed 不同）→ 成功进入 `awaiting_confirm`。
- 重试仍 805 → 段置 `failed` 并携带错误原因交用户 `regenerate`。

### 5. chain.json 断点续跑　✅（含 1 项边界观察）
- **正常恢复**：段 1 处于 `awaiting_confirm` 时重建编排器，`chain.json` 正确恢复 `current_index`/段产物路径，可续跑不重头。
- **边界观察（G1）**：段残留 `submitting`（崩溃窗口）时，`submit_segment` 幂等返回（视为 in-flight 不重提）、`regenerate_segment` 抛 `TASK_STATE_INVALID` → **该段无自动恢复路径**。详见 §五。

### 6. 逐段确认状态机　✅
- `accept` 非 `awaiting_confirm` 段（如 `pending`）→ `KeyError(TASK_STATE_INVALID)`。
- `submit` 幂等：`awaiting_confirm`/in-flight/accepted 段不重复提交、不换 seed。
- `regenerate` 自动换随机 seed、覆盖本段产物、首帧来源不变。

### 7. 尾帧提取（PNG）　✅
- `extract_tail_frame` 输出 PNG，落盘 `frames/tail_{i:03d}.png`。
- **失败路径证伪**：`extract_tail_frame` 抛 `FFmpegError` → 段置 `failed`，**不自动提交下一段**（链中断，交用户重生成）——符合 PRD §7「尾帧提取失败 → 不进入下一段」。

### 8. mock 零成本路径　✅
- `MockAdapter.generate_i2v` 纯本地 FFmpeg 占位，`httpx.post/stream/get` **零调用**（无外网），产出可播放 mp4。
- E2E 全流程（start → 逐段 accept → concat → final_video.mp4）在 mock 下跑通。

### 9. AC16 残留检索　✅
- `app/` 目录全局检索 `ImageGenerator/EngineSwitcher/image_* 四适配器/IMAGE_ADAPTER_REGISTRY/ImageProvider/generate_image/supports_first_frame/build_anchor_prompt` → **零残留**。
- 命中的 `tests/test_consistency_adversarial.py`、`test_runninghub_adversarial.py`、`test_config_store.py` 均为**删除彻底性的负向断言**（符合预期）；`docs/` 命中的是删除清单说明；`config/config.json` 遗留的旧 `image_*` 键为**数据残留**（被 `extra="ignore"` 静默忽略，下次 save 才清除），非代码引用。

### 10. 零外链 / 零新依赖 / 铁律　✅
- 前端 `index.html`/`app.js`：`<link href="/static/style.css">`、`<script src="/static/app.js">`、`fetch` 全部指向**相对路径**，无任何 `src/href/fetch/@import/url()` 指向外域（仅 placeholder 文本、帮助文案中出现 `http(s)` 字样，非资源加载）。
- `requirements.txt` 仍 6 项既有包，**零新增**。
- `app/` 无 `shell=True`（仅 docstring 说明「不使用」）、无 `E:\` 绝对路径泄漏（仅 docstring 说明「禁止暴露」）、`StageName.ALL` 恒 5（story→script→director→concat→critic）。

---

## 四、测试基线汇总

| 指标 | 结果 |
|---|---|
| 全量单测 | **Ran 384 tests → OK**（93.085s） |
| E2E | **63/63 通过**，exit 0，config.json md5 未变 |
| node --check | **exit 0** |
| QA 对抗脚本 | **PASS=42 / FAIL=0** |

---

## 五、已知观察项（非阻断）

### G1（边界观察）：`submitting` 段无自动恢复路径
- **现象**：`chain.json` 中某段残留 `submitting`（进程在 `_set_state(SUBMITTING)` 与最终 `_save()` 之间崩溃的窗口）时，重建编排器后 `submit_segment` 因状态属 `_IN_FLIGHT_STATES` 幂等返回（不重提），`regenerate_segment` 因仅允许 `awaiting_confirm`/`failed` 而抛 `TASK_STATE_INVALID` → 该段无法续跑/重生成。
- **影响评估**：极窄——仅在「提交到 RunningHub → 轮询 → 下载」这整段同步窗口内发生进程崩溃才会残留；当前架构为单进程内存任务表（`TaskManager` 进程重启即清空），跨进程断点续跑本就不完整，故此状态在当前正常流程**不可达**。
- **建议（非必须）**：若未来实现跨进程 `chain.json` 重载，可在 `_load()` 时将历史 `submitting`/`uploading`/`polling` 状态归一为 `pending`（或 `failed` + 提示重生成），并删除未使用的 `UPLOADING`/`POLLING` 死枚举或补全其状态迁移。
- **裁定**：不阻断，记录备查。

### G2（轻微健壮性）：805 判定为子串匹配
- `_is_805` 用 `"805" in message`，理论上会误命中 `1805`/`8050` 等子串。当前 RunningHub 业务码为独立 `errorCode` 字段、消息含 `errorCode=805`，误命中概率极低，不构成缺陷。

---

## 六、智能路由判定依据

1. 全量单测 384 OK、E2E 63/63、`node --check` 通过 → 工程师基线复现一致。
2. QA 独立对抗 42 项断言**全部通过**，未发现「测试正确但实现错误」的源码缺陷。
3. 过程中曾出现的 3 处 FAIL 经复核均为 **QA 自身断言瑕疵**（`video_model` 误判为应删除、`shell=True`/`E:\` 未排除 docstring），已修正后重跑全绿 —— 属「测试 bug → QA 自行修复」路径，非源码问题。

**最终：Routing = NoOne。**

---

## 七、交付物

- 本报告：`docs/测试报告-RunningHub工作流首帧链.md`
- QA 独立对抗脚本：`tests/qa_runninghub_adversarial.py`（42 项断言，独立于工程师 384 测试，`qa_*` 前缀不参与 `unittest discover`）
