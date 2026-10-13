# 测试报告：视频 Provider 体系替换为 RunningHub

- **验证人**：Edward（QA Engineer，独立对抗式复核）
- **被验对象**：视频生成 provider 体系替换（删除可灵/即梦/MiniMax/通用适配器，全部改用 RunningHub 模型 API）
- **需求原话**：「删去可灵、即梦、minimax 接口，视频生成接口全部用 runninghub 的 api 接口来生成」
- **验证日期**：2026-10-07（含 P2-1 修复复核，第二轮）
- **工程师前置声明**：IS_PASS = YES（268 单测 + e2e 56/56）——本报告**不复述其结论**，一切以独立复现为准

---

## 0. TL;DR

| 项 | 结论 |
|---|---|
| **总体判定** | ✅ **通过**（无法证伪） |
| **P0（契约不合官方）** | **未能证伪**：对照 RunningHub **官方英文文档**逐字段核对，端点/字段名/枚举**逐字一致** |
| **P1（成本/错误码/迁移）** | **未能证伪**：探针零任务、errorCode 空值不误判、旧 provider 迁移不脏化 |
| **P2-1（非法 provider 重置整份配置）** | ✅ **已修复并复核通过**：任意非法值（含显式 `null`/非字符串）均回退 `mock` 且其它字段逐字保留；`config_store.py` 已**零 provider 字面量**（详见 §5） |
| **P2-2 / P2-3** | 构建产物 / 历史日志，**不阻塞** |
| **必须修问题** | **0 个（P0/P1）** |
| **Routing Decision** | **NoOne**（无缺陷需回炉） |
| **新增对抗测试** | `tests/test_runninghub_adversarial.py`（**42 用例**，全绿） |
| **全量回归** | `Ran 313 tests ... OK`（271 基线[含 config_store +3] + 42 新增） |
| **e2e** | `56/56 通过`，exit 0，`config.json` md5 前后不变 |
| **受限未做** | **未调用真实 RunningHub API**（会花钱）——契约以官方文档核对 + 本地桩行为验证 |

---

## 1. 实际执行的命令与关键原始输出

> 全部测试均在**禁代理**环境下执行（本机 `http_proxy=127.0.0.1:55069` 会把 127.0.0.1 连接被拒污染成 HTTP 502 → 假失败）：

```bash
# ① 全量回归（新对抗文件已并入）——P2-1 修复后（第二轮）
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  .venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
# —— 原始输出 ——
# Ran 313 tests in 112.014s
# OK
# （第一轮基线为 304：[268 基线] + [36 对抗]；本轮工程师 config_store +3、我方对抗 +6 → 313）

# ② 单独跑工程师适配器测试（核对「23」声明）
... -m unittest tests.test_runninghub_adapter
# Ran 23 tests in 15.548s
# OK

# ③ 单独跑配置迁移测试（P2-1 修复后应为 15）
... -m unittest tests.test_config_store
# Ran 15 tests / OK

# ④ e2e 冒烟（用 run_e2e.sh 自带的 unset http_proxy，不额外注入）
cd /e/ai/storyforge && bash tests/run_e2e.sh
# ===== E2E 汇总：56/56 通过 =====
# E2E exit code = 0
# 已还原 config.json（"llm_base_url": "http://127.0.0.1:8080/v1"）

# ⑤ config.json 完整性（e2e 前后）
md5sum config/config.json
# BEFORE md5: a7f595d970eb90814e6c16fe002b4365
# AFTER  md5: a7f595d970eb90814e6c16fe002b4365   ← 未污染

# ⑥ 前端脚本语法
node --check app/static/app.js
# JS_OK

# ⑦ 新增对抗套件（P2-1 修复后）
... -m unittest tests.test_runninghub_adversarial
# Ran 42 tests in 16.866s
# OK

# ⑧ P2-1 残留扫描：config_store.py 已零 provider 字面量
grep -in "kling\|seedance\|minimax\|generic\|hailuo\|_REMOVED_VIDEO_PROVIDERS" app/services/config_store.py
# （No matches found → CLEAN）
```

**关于 e2e 的代理**：`tests/run_e2e.sh` 第 15 行已内置
`unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy`，
故「直接 `bash tests/run_e2e.sh`」即可稳定得到 56/56（本次实测确认）。脚本还会在入口探测 8123/18080 端口、
起服务后自检进程存活 + 检查 app 日志无 bind 错误，杜绝「连到残留旧服务」的假就绪。

---

## 2. P0 —— 契约是否符合官方（**自行查证，未采信工程师转述**）

**查证来源（官方 API 文档，非社区/非工程师）**：
- 中文官方页：`https://www.runninghub.cn/runninghub-api-doc-cn/api-448183147`
  （「全能视频V3.1-Lite文生视频-官方稳定版」）
- 英文官方页：`https://www.runninghub.ai/runninghub-api-doc-en/api-448184432`
  （`google/veo3.1-lite/text-to-video-official-stable`，同一端点 `rhart-video-v3.1-lite-official/text-to-video`）
- 官方查询端点：`https://www.runninghub.cn/openapi/v2/query`

**官方英文文档原文（关键字段）**：

```
POST /openapi/v2/rhart-video-v3.1-lite-official/text-to-video
Body Params application/json
  prompt        string required  (>= 1 characters)
  aspectRatio   enum   Allowed values: 16:9 9:16        Default: 16:9
  resolution    enum   Allowed values: 720p 1080p       Default: 1080p
  duration      enum   Allowed values: 4 6 8            Default: 6
  seed          integer optional (>= -1)
Response: status enum Allowed values: QUEUED RUNNING SUCCESS FAILED
          errorCode string / errorMessage string
          results array[object]|null → url string|null
查询接口：/openapi/v2/query   body {"taskId": "..."}
```

**逐字段核对（官方文档 ↔ 源码 `app/services/adapters/runninghub_adapter.py`）**：

| 契约项 | 官方文档 | 源码常量/行为 | 结论 |
|---|---|---|---|
| 提交端点 | `/openapi/v2/rhart-video-v3.1-lite-official/text-to-video` | `DEFAULT_ENDPOINT` 同值 | ✅ 逐字一致 |
| Base（国内/国际） | `www.runninghub.cn` / `www.runninghub.ai` | `DEFAULT_BASE_URL="https://www.runninghub.cn"`，注释含国际站 | ✅ |
| 字段名 | `prompt`/`aspectRatio`/`resolution`/`duration` | `_submit` body 恰为这 4 个键 | ✅ |
| `duration` 类型 | 字符串（示例 `"6"`） | `str(int(duration))` | ✅ |
| `duration` 枚举 | **仅 4 / 6 / 8** | `_ALLOWED_DURATIONS=(4,6,8)`，就近钳制 | ✅ |
| `resolution` 枚举 | `720p` / `1080p` | `_ALLOWED_RESOLUTIONS=("720p","1080p")` | ✅ |
| `aspectRatio` 枚举 | `16:9` / `9:16` | `_RATIO="16:9"` + `_map_ratio`（宽≥高→16:9 否则 9:16） | ✅ |
| 鉴权 | `Authorization: Bearer <KEY>` + `Content-Type: application/json` | `_headers()` 同值 | ✅ |
| 查询端点 | `/openapi/v2/query`，body `{"taskId":...}` | `QUERY_PATH`+`_poll` 同值 | ✅ |
| 状态枚举 | `QUEUED/RUNNING/SUCCESS/FAILED` | `_poll` 判 `SUCCESS`/`FAILED`，其余继续轮询 | ✅ |
| 下载地址 | `results[0].url` | `_results_url` 取 `results[0].url`（兼容元素为字符串） | ✅ |

> **team-lead 特别点名**：不同模型端点的宽高比字段名不同（如 `seedance-2.0-mini` 用 `ratio`，`kling` 系列用 `aspectRatio`）。
> 经查证：**本次默认端点 `rhart-video-v3.1-lite-official` 官方字段确为 `aspectRatio`**（非 `ratio`），源码选择正确。

**结论**：P0 **无法证伪**。源码与官方文档在端点、字段名、枚举、鉴权、查询、响应解析上**全部一致**。
（唯一妥协：为省费用**未做真实 API 调用**；契约正确性以官方文档 + 本地桩驱动真实 `httpx` 请求捕获路径/请求体来验证。）

---

## 3. P1 —— 成本安全 / 错误码语义 / 迁移不脏化

### 3.1 成本安全（探针绝不创建任务）

- **源码级**：`runninghub_adapter.py` 中 `httpx.post(` **恰 2 处**（`test_connection` 探针 + 通用 `_post_json`），
  `httpx.stream(` **恰 1 处**（`_download`）；`test_connection` 函数体内引用 `QUERY_PATH` 且**不引用** `self.endpoint`（提交端点）。
- **行为级**（本地桩）：`test_connection()` → `submit_bodies == []`（未打提交端点）、`query_calls == 1`、
  查询体**恰为** `{"taskId": "0"}`；无 Key 时 `query_calls == 0`（不发任何请求）。
- 结论：探针**零任务创建**，不会花钱。✅

### 3.2 errorCode 语义（不得误判）

`_extract_error` 矩阵实测：

| 输入 | 输出 | 判定 |
|---|---|---|
| `{}` | `("", "")` | 不失败 |
| `{"errorCode": None, "errorMessage": None}` | `("", "")` | 不失败 |
| `{"errorCode": "", "errorMessage": ""}` | `("", "")` | 不失败 |
| `{"errorCode": 0}` / `{"errorCode": "0"}` | `("0", "")` | 不失败（`_NO_ERROR_CODES` 含 `"0"`） |
| `{"errorCode": "  "}` | `("", "")` | 不失败（strip） |
| `{"errorCode": "1001", ...}` | `("1001", ...)` | **立即失败** |

- 桩返回 `RUNNING + errorCode ∈ {"0","","缺失",None}` → 适配器**不判失败**，继续轮询至超时（行为验证）✅
- 桩返回 `RUNNING + errorCode="1001"` → **立即** `VID_GENERATE_FAILED`，报错含 `1001`、**不含**「超时」✅
- 提交端点返回 `errorCode="0"` → 不误判（桩固定如此，仍正常走完流程）✅

### 3.3 迁移不脏化（team-lead 最担心的一条）

> **第二轮更新**：工程师已修复 P2-1（删除白名单常量，改为「凡非法即回退」+ 显式 `null` 单独处理）。
> 本节反映**修复后**的最终行为。

手工构造**含全部字段**的 `config.json` 走**真实加载路径**，覆盖**任意非法** `video_provider`，
逐字段断言「回退 `mock` 且其它字段逐字保留」：

```
非法输入矩阵（全部 → video_provider="mock"）：
  · 旧下线值：kling / seedance / minimax / generic
  · 拼写错误：foo / runninbghub
  · 大小写 / 尾空格：RUNNINGHUB / "mock "
  · 非字符串：123 / 4.5 / True / [] / {"a":1}
  · 显式 null：None
键缺失        → 默认 mock
合法值 mock/runninghub → 原样保留（不回退）
每种情形均断言：其余 12 字段（llm_mode/llm_base_url/llm_api_key/llm_model/
  video_api_key/video_base_url/video_model/resolution/style/
  shot_count/shot_duration/skip_video_eval）与输入逐字相同
反向断言：llm_model ≠ "qwen2.5:14b"、shot_count ≠ 4、video_api_key ≠ ""（证明未整份重置）
```

- **显式 `null`**（重点）：`{video_provider: null, llm_mode:openai, llm_model:deepseek-chat, shot_count:6, shot_duration:9}`
  → 加载后 `video_provider="mock"`、其余字段**全部保留**（实测确认）✅
- 合法值 `mock` / `runninghub` **不被回退** ✅
- 日志确证：`检测到非法的视频提供方 'foo'（合法值：mock/runninghub），已自动回退为 mock（其它配置保持不变）` ✅

**分层设计复核**（helper `_migrate_removed_providers` + `_drop_null_video_provider`）：
- **顺序/覆盖**：helper 先跑（非法值改写 mock、`None`/缺失**不动**）→ drop 后跑（**仅**删除显式 `null` 键）
  → Pydantic 落默认。两函数作用域互斥（helper 不改 `None`，drop 不改非 `None`），**无覆盖漏洞**；
  即便调换顺序亦安全（drop 删键后 helper 见「缺键」直接返回）。
- **是否「为迁就测试而拆分」**：**判定为可接受**。拆分保留了 helper 级「`None→None` 恒等」语义
  （我方冻结断言 `test_non_string_provider_not_touched_by_helper` 仍通过），并把「非法已给值」与
  「显式 null 视同缺省」两种语义显式区分——属**轻微冗余**（单函数亦可），但**正确、有测试、无副作用**，
  **不构成回炉理由**。

**结论**：迁移**不脏化**，已覆盖旧下线值 + 任意拼写错误/垃圾值/非字符串 + 显式 `null`。✅

> **边界（如实记录，非本次目标）**：本修复仅作用于 `video_provider` 字段。
> 若**其它**字段同时非法（如 `shot_count: 999` 越界），Pydantic 仍会整份重置
> （实测：`{video_provider:null, shot_count:999}` → `llm_model` 亦被重置为默认）。
> 该行为属**既有**设计，超出本次 P2-1 范围（team-lead 明确不扩大范围），仅作记录。

---

## 4. P2 —— 零外网 / 删除彻底性 / 前端接线

### 4.1 零外网

以 `mock.patch` 包裹 `httpx.post`/`httpx.stream`，对每次请求 **断言 host ∈ {127.0.0.1, localhost}**；
完整跑 `generate()`（提交→轮询→下载）+ `test_connection()` 后，所有请求主机**均为 loopback**。✅

### 4.2 删除彻底性（源码级扫描）

| 扫描项 | 结果 |
|---|---|
| `app/models/enums.py`：`VideoProvider` 取值 | 仅 `{mock, runninghub}` ✅ |
| `app/services/adapters/`：目录文件 | 仅 `__init__.py / base.py / mock_adapter.py / runninghub_adapter.py` ✅ |
| `ADAPTER_REGISTRY` 键 | 仅 `{MOCK, RUNNINGHUB}` ✅ |
| `import app.services.adapters.{kling,seedance,minimax,generic}_adapter` | 全部 `ModuleNotFoundError` ✅ |
| `app/**.py` 扫描 token `kling/seedance/minimax/generic/hailuo` | **无任何残留**（`config_store.py` 已 **CLEAN**）✅ |
| `app/static/{index.html,app.js}` 扫描同上 | 无残留 ✅ |
| `docs/` 是否有 MiniMax 报告 | 无 ✅ |
| `tests/test_*.py` 是否有 MiniMax 测试 | 无 ✅ |
| `README.md` 残留 | 无 ✅ |

> **第二轮更新（最终状态）**：工程师已删除白名单常量 `_REMOVED_VIDEO_PROVIDERS`，
> 改为从枚举派生 `_ALLOWED_VIDEO_PROVIDERS = frozenset(p.value for p in VideoProvider)`。
> 经实测 `grep kling/seedance/minimax/generic/hailuo/_REMOVED_VIDEO_PROVIDERS app/services/config_store.py`
> → **No matches found**：`app/services/config_store.py` 现为**零 provider 字面量**。
> 仅**两处测试文件**（`tests/test_config_store.py`、`tests/test_runninghub_adversarial.py`）**刻意**保留
> 旧 provider 字面量作为迁移用例的**输入数据**——这是测试数据，不是生产代码残留。

### 4.3 现有断言未被削弱

- 全量 **313** 用例全绿；`test_frontend_consistency`（阶段卡=5、`$()` id 全存在、零外链）保持通过 ✅
- 工程师的 23 个适配器用例全部保留并独立复跑通过 ✅
- 我方 `test_runninghub_adversarial.py` 由 36 增至 **42**（为复核 P2-1 新增 6 条，**未削弱任何既有断言**）✅

### 4.4 前端接线

- `index.html`：`name="video_provider"` 单选**恰 2 个**（`mock` 默认选中 / `runninghub`）✅
- 文案含「Base URL 留空默认国内站」「模型 API 需『企业级-共享』Key」「文生视频不支持首帧参考图」等正确提示 ✅
- `app.js`：`selectedRadio("video_provider")` 读取 + `provider === "mock"` 联动（mock 时禁用配置项）✅
- `app.js` 中所有 `$("id")` 引用在 `index.html` 均存在（无悬空引用）✅
- `node --check app/static/app.js` → 语法 OK ✅

---

## 5. 边界与遗留

| 编号 | 现象 | 状态 | 严重度 | 结论 |
|---|---|---|---|---|
| **P2-1** | 仅白名单 4 个已知历史值；其它非法值（如 `"foo"`、数字、**显式 `null`**）走「整份配置重置」路径，丢失 LLM/画幅设置 | ✅ **已修复** | 原 P2 | **复核通过**：删除白名单 → 派生枚举；「凡非法即回退」+ 显式 null 单独处理。矩阵（14 类非法输入）+ 组合 + 真实加载路径全部断言通过，其余字段逐字保留（详见 §3.3） |
| P2-2 | `build/`、`dist/`（PyInstaller 产物）为**旧的**打包快照，`dist/StoryForge/_internal/app/static/index.html` 仍含旧 provider 文案 | 未改动 | P2 | 不影响源码运行；发布前重新打包即可 |
| P2-3 | `logs/app.log` 保留大量历史 MiniMax 运行日志 | 未改动 | P3 | 仅历史日志，非源码，无需处理 |

> **P2-1 修复前证据（留档）**：`{"video_provider":"foo","llm_model":"keep-me","shot_count":7}` → 加载后
> `provider=mock 但 llm_model=qwen2.5:14b、shot_count=4`（整份被重置）。
> **修复后**：同一输入 → `provider=mock、llm_model="keep-me"、shot_count=7`（其它字段保留）。

---

## 6. 交付物

| 文件 | 说明 |
|---|---|
| `tests/test_runninghub_adversarial.py` | 对抗测试 **42 用例**（7 组：契约 / 成本 / 错误码 / 迁移 / **迁移加固** / 零外网 / 删除+前端），全绿 |
| `docs/测试报告-视频Provider替换为RunningHub.md` | 本报告（含 P2-1 修复复核） |

**未改动**：`config/config.json`（md5 前后一致）、`start.bat`、`start_tunnel.bat`、任何被测源码。

---

## 7. Routing Decision

**→ NoOne**

- P0（契约）**无法证伪**：源码与 RunningHub **官方文档**逐字段一致；
- P1（成本安全 / errorCode 语义 / 迁移不脏化）**无法证伪**；
- **P2-1 已修复并独立复核通过**（含显式 `null` 与非字符串）；P2-2/P2-3 为构建产物/历史日志，不阻塞交付。
- 全量 **313** 单测 + e2e 56/56 全绿，`config.json` 未被污染（md5 不变）。

---

## 8. 增补：base_url 双路径(1001) 修复 + H3 家族参数适配

- **触发**：用户真实出片 4 镜全失败 —— `VID_API_ERROR: RunningHub 提交生成任务失败（errorCode=1001，errorMessage：Invalid URL, please check your link）`，当时 `video_base_url` 直接粘了**完整模型端点** `…/openapi/v2/minimax/hailuo-h3/text-to-video`、`video_model` 留空。
- **本轮被验改动**：`app/services/adapters/runninghub_adapter.py`（`_split_url` / 家族感知参数）、`tests/test_runninghub_adapter.py`（`TestEndpointResolution` / `TestH3Family`）、`app/static/index.html`、`docs/使用说明.md`、`README.md` 文案，以及 `tests/test_runninghub_adversarial.py` 的残留守卫**收窄**。
- **本轮验证人**：Edward（QA，独立证伪；**不采信工程师/TL 转述**）。

### 8.0 TL;DR

| 项 | 结论 |
|---|---|
| **A 端点解析（双路径）** | ✅ **无法证伪**：枚举 12 类填法（空/纯主机/裸主机/尾斜杠/查询串/fragment/完整端点进 base/`..`/`//`/大小写/前后空格/model 路径/双 URL），**无一双路径**；用户原填法实测提交路径恰为 H3 端点 |
| **B H3 参数体系** | ✅ **全部通过**：H3 体**恰** `{prompt,ratio,resolution,duration}`（无 `aspectRatio`）；`ratio` 恒 `16:9`/`9:16`（**非** `adaptive`）；`duration` 为字符串 ∈5–15；非 H3 端点**零污染** |
| **C 真实接口零成本契约** | ✅ **实测坐实**：`1001=Invalid URL`；H3 分辨率枚举 `2K,768P`、默认端点 `720p,1080p`、H3 时长枚举 `5..15`；**每探测均 `taskId` 为空、未建单、未计费** |
| **守卫收窄裁定** | ✅ **合理收窄，非削弱**：裸 provider 值（`"minimax"`/`"hailuo"`/…）剥离后**仍被抓**（详见 §8.4） |
| **D 回归** | ✅ 全量 `Ran 329 tests … OK`（基线 324 + 本轮证伪 +5）；e2e `56/56`、exit 0、`config.json` md5 前后恒为 `3a23fc3d09a0c8dce57066d6ec501bfb` |
| **新发现问题** | **0 个 P0/P1**；仅 2 处**低危观察**（见 §8.5，非回归） |
| **Routing Decision** | **→ NoOne** |
| **付费不确定性** | 见 §8.6：仅「一次真实出片」需用户授权，非阻塞 |

### 8.1 A —— 端点解析（重点：任何填法都不得双路径）

方法：直接用真实 `Config(...)` + `RunningHubAdapter(...)` 实例化，检查解析后的
`adapter.base_url`（应恒为**纯 origin**）与 `adapter.endpoint`（应恒为**单段路径**）。

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY .venv/Scripts/python.exe  # 内联脚本
```

| base_url 输入 | model | 解析 base_url | 解析 endpoint | 双路径? |
|---|---|---|---|---|
| `""` | `""` | `https://www.runninghub.cn` | `/openapi/v2/rhart-video-v3.1-lite-official/text-to-video` | 否 |
| `https://www.runninghub.cn` | `""` | `https://www.runninghub.cn` | 默认端点 | 否 |
| `https://www.runninghub.cn/` | `""` | `https://www.runninghub.cn` | 默认端点 | 否 |
| `www.runninghub.cn`（裸主机） | `""` | `https://www.runninghub.cn` | 默认端点 | 否 |
| `…/minimax/hailuo-h3/text-to-video`（★用户填法） | `""` | `https://www.runninghub.cn` | `/openapi/v2/minimax/hailuo-h3/text-to-video` | 否 |
| （同上）+ 尾斜杠 `/` | `""` | `https://www.runninghub.cn` | 同上（尾斜杠被规范化） | 否 |
| （同上）+ `?a=1`（查询串） | `""` | `https://www.runninghub.cn` | 同上（查询串被丢弃） | 否 |
| （同上）+ `#frag`（fragment） | `""` | `https://www.runninghub.cn` | 同上（fragment 被丢弃） | 否 |
| `https://www.runninghub.cn/openapi/v2/query`（填成查询端点） | `""` | `https://www.runninghub.cn` | `/openapi/v2/query`（**单**路径，但语义错误=用户误填） | 否 |
| `https://www.runninghub.cn//openapi//v2//x/text-to-video` | `""` | `https://www.runninghub.cn` | `//openapi//v2//x/text-to-video`（重复斜杠保留） | 否 |
| `https://www.runninghub.cn/openapi/v2/../v2/x/text-to-video` | `""` | `https://www.runninghub.cn` | `/openapi/v2/../v2/x/text-to-video`（`..` 未规范化） | 否 |
| `HTTPS://WWW.RunningHub.CN` | `""` | `https://WWW.RunningHub.CN`（netloc 大小写保留） | 默认端点 | 否 |
| `  https://www.runninghub.cn  `（前后空格） | `""` | `https://www.runninghub.cn` | 默认端点 | 否 |
| `https://www.runninghub.cn` | `openapi/v2/x/text-to-video`（无前导斜杠） | `https://www.runninghub.cn` | `/openapi/v2/x/text-to-video` | 否 |
| `https://www.runninghub.cn` | `/openapi/v2/x/text-to-video` | `https://www.runninghub.cn` | 同上 | 否 |
| `https://a.example.com/openapi/v2/a/text-to-video` | `openapi/v2/b/text-to-video` | `https://a.example.com` | `/openapi/v2/b/text-to-video` | 否 |
| `https://www.runninghub.cn` | `https://b.example.com/openapi/v2/b/text-to-video`（完整 URL，应取胜） | `https://b.example.com` | `/openapi/v2/b/text-to-video` | 否 |

**结论**：**无法证伪**。设计上 `base_url` 在**所有分支**都被归一化为 `scheme://netloc`（无 path），`endpoint` 为单段路径，故 `base_url+endpoint` 只可能含**一段** `/openapi/v2/`。用户原填法经桩服务实测，真实提交 path **恰为** `/openapi/v2/minimax/hailuo-h3/text-to-video`，且**不含** `/openapi/v2/rhart`（即未被追加默认端点）。

### 8.2 B —— H3 家族参数体系

| 断言 | 结果 |
|---|---|
| H3 提交体键集合 **恰** `{prompt, ratio, resolution, duration}`，**无** `aspectRatio` | ✅ `{'prompt','ratio','resolution','duration'}` |
| `ratio`（横屏 1280x720/1920x1080 → `16:9`；竖屏 720x1280/1080x1920 → `9:16`）；**永不为** `adaptive` | ✅ 五点取值全对，`adaptive` 未出现 |
| `duration` 恒为**字符串**且 ∈ 5..15（输入 2/4 上钳到 `5`；平局取下限） | ✅ 2→5 4→5 5→5 … 15→15 16→15 |
| H3 分辨率映射 `720p→768P`、`1080p→2K` | ✅ |
| 非 H3（默认）端点**未被污染**：`{prompt, aspectRatio, resolution, duration}` + `720p/1080p` + `4/6/8` | ✅ 2→4 5→4 7→6 10→8 |

### 8.3 C —— 真实接口零成本契约校验（**已禁代理，绝不建单**）

执行前显式 `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy`，并对 `httpx` 传 `trust_env=False`，全程只打国内站。**手法**：只把**单个字段**置为非法值 `"__BAD__"`，若回显**只**抱怨该字段 → 证明其余字段均已通过校验；每步校验 `taskId` 是否为空，非空即中止。

```text
# C1 不存在的模型路径 → 证「1001 == 路径无效」（用户原始报错的根因）
POST https://www.runninghub.cn/openapi/v2/no-such-model-xyz/text-to-video
  {"prompt":"x","ratio":"16:9","resolution":"768P","duration":"6"}
← {"taskId":"", "errorCode":"1001",
   "errorMessage":"Invalid URL, please check your link | 请求链接无效，请检查您的调用链接", ...}

# C2 H3 端点，resolution 故意非法 → 枚举 2K/768P；其余字段全部通过
POST https://www.runninghub.cn/openapi/v2/minimax/hailuo-h3/text-to-video
  {"prompt":"x","ratio":"16:9","resolution":"__BAD__","duration":"6"}
← {"taskId":"", "errorCode":"1007",
   "errorMessage":"field 'resolution' value __BAD__ is not in the allowed options, allowed values: 2K, 768P", ...}

# C3 默认端点，resolution 故意非法 → 枚举 720p/1080p
POST https://www.runninghub.cn/openapi/v2/rhart-video-v3.1-lite-official/text-to-video
  {"prompt":"x","aspectRatio":"16:9","resolution":"__BAD__","duration":"6"}
← {"taskId":"", "errorCode":"1007",
   "errorMessage":"field 'resolution' value __BAD__ is not in the allowed options, allowed values: 720p, 1080p", ...}

# C4 H3 端点，duration 故意非法 → 枚举 5..15（与部分第三方文档的 4–15 不同）
POST https://www.runninghub.cn/openapi/v2/minimax/hailuo-h3/text-to-video
  {"prompt":"x","ratio":"16:9","resolution":"768P","duration":"__BAD__"}
← {"taskId":"", "errorCode":"1007",
   "errorMessage":"field 'duration' value __BAD__ is not in the allowed options, allowed values: 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15", ...}
```

**结论**：
1. `1001` 语义 = **URL/路径无效** → 坐实用户原始失败是「双路径」而非 Key/配额问题；
2. 每个探针的回显**只**抱怨被故意置错的字段 → **证明 C2/C3/C4 中其余字段（`prompt`/`ratio`/`aspectRatio`/`resolution` 合法值/`duration` 合法值）全部通过服务端校验**，即两套参数体系与字段名**真实可用**；
3. 三条探测 `taskId` **均为空**、`status` 均为空 → **未创建任何任务、零计费**；
4. 探针全程无 `401/433` → 该 Key 鉴权正常。

### 8.4 裁定 —— `tests/test_runninghub_adversarial.py` 的守卫「收窄」是否正当？

**背景**：`test_app_source_has_no_residue_except_migration` 原对 `app/**.py` 做 `tok in text.lower()`（tok=`kling|seedance|minimax|generic|hailuo`）。因 H3 **端点路径**本身含 `minimax/hailuo-h3`，产生**语义误伤**。TL 新增 `_MODEL_NAME_ALIASES` + `_strip_model_aliases()`，扫描前剥离合法**模型名**别名。

**独立实测**（对 `_strip_model_aliases` 逐一喂入合成串）：

| 输入（合成） | 剥离后残留 token | 判定 |
|---|---|---|
| `VideoProvider.MINIMAX = "minimax"`（**裸 provider 值**） | `['minimax']` | ✅ **仍被抓** |
| `VideoProvider.HAILUO = "hailuo"` | `['hailuo']` | ✅ **仍被抓** |
| `provider = "kling"` / `"seedance"` / `GenericAdapter()` | 对应 token | ✅ **仍被抓** |
| `HAILUO_MODEL = "hailuo"` | `['hailuo']` | ✅ **仍被抓** |
| `"/openapi/v2/minimax/hailuo-h3/text-to-video"` | `[]` | ✅ 合法模型名，不误伤 |
| `# MiniMax-H3 家族` / `_H3_PATH_MARKER = "hailuo-h3"` | `[]` | ✅ 合法模型名，不误伤 |

**裁定**：**合理的语义收窄，非削弱**。
- 别名集**不含任何裸 provider 值**（`kling`/`seedance`/`minimax`/`generic`/`hailuo` 单独出现必被抓）；
- 唯一能「逃逸」的写法是把残留**逐字写成模型名别名**（如值 `== "minimax/hailuo-h3"`）——那**在语义上就是模型名**，不是「已下线 provider 残留」；
- 故「有人把 provider 枚举值写成 `"minimax"` 或 `"hailuo"`」这一情形，守卫**依然能抓**，**不构成 P1**。
- **附带观察（低危）**：6 个别名中仅 `minimax/hailuo-h3`、`minimax-h3`、`hailuo-h3` 在 `app/` 中**确被用到**（`grep` 实测仅 5 处、均属该三类）；`minimax h3` / `hailuo 3` / `hailuo-03` 三者为**无来源的宽别名**，虽不影响正确性，但每多一条即多一分盲区，建议注明来源或删除。

> 为把「不削弱」锁死，我方新增 `TestGuardNarrowingSafety`（3 例）：断言裸 provider 值仍被抓、合法模型名不被误伤、**别名集不得含裸 provider 值**（防止未来有人把别名拓宽到裸值而静默掏空守卫）。

### 8.5 边界与新发现（均**非**回归）

| 编号 | 现象 | 严重度 | 结论 |
|---|---|---|---|
| O-1 | `base_url` **只有路径无主机**（如 `/openapi/v2/x/text-to-video`）→ 补 `https://` 后 netloc 为空，`base_url="https://"`，最终 URL `https:///openapi/…` 畸形 | P3 | 界面语义为「主机/完整端点」，此填法非常规；**不产生双路径**，仅报错不友好。仅记录，不阻塞 |
| O-2 | `base_url` 填成查询端点 `…/openapi/v2/query` → `endpoint` 取 `/openapi/v2/query`，**单路径**，但语义错误（提交到查询端点） | P3 | 用户误填，非解析缺陷；服务端会返回可读错误。仅记录 |
| O-3 | `base_url` 含 `..` 或重复 `//` 时**不做规范化**，按原样拼接 | P3 | 非常规填法，**不产生双路径**；服务端可能 1001/404。仅记录 |
| O-4 | 残留守卫对**无分隔符命名**（如类名 `MiniMaxH3Adapter`）仍会误报 `minimax` | P3 | 属**误报（false positive）**而非漏报，方向安全；当前 `app/` 无此类命名，不阻塞 |

**未发现**任何仍会产生**双路径**的输入；**未发现**任何 P0/P1 缺陷。

### 8.6 付费不确定性（需用户授权，非阻塞）

本轮已用**零成本手法**坐实端点、字段名与三处枚举（C2–C4 均 `taskId` 空、未计费）。唯一**无法零成本确认**的是「全字段合法时能否真正建单并出片」——该动作会**真实计费**，**未执行**。如需最终签收，建议由用户**授权一次真实出片**（1 镜、最短时长）后核对 `taskId` 非空与 `outputs/**/*.mp4` 落盘。

### 8.7 D —— 回归与静态核对（原始输出）

```bash
# ① 全量单测（禁用全部代理；含本轮 +5 独立证伪用例）
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy \
  .venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
# Ran 324 tests in 115.422s   → OK        ← 本轮加用例前的基线（与交接预期一致）
# Ran 329 tests …             → OK        ← 新增 TestEndpointNoDoublePathFalsification(+2)
#                                            + TestGuardNarrowingSafety(+3) 之后

# ② e2e 冒烟（run_e2e.sh 自带 unset 代理）
cd /e/ai/storyforge && bash tests/run_e2e.sh
# ===== E2E 汇总：56/56 通过 =====
# E2E exit code = 0
# 已还原 config.json（"llm_base_url": "http://127.0.0.1:8080/v1"）

# ③ config.json 完整性（e2e 前后）
md5sum config/config.json
# BEFORE: 3a23fc3d09a0c8dce57066d6ec501bfb
# AFTER : 3a23fc3d09a0c8dce57066d6ec501bfb   ← 未被污染（含用户真实 Key/端点，未改动）

# ④ 前端 / 常量 / 探针
node --check app/static/app.js                       # → app.js syntax OK
grep -inE "kling|seedance|minimax|generic|hailuo" app/static/index.html   # → none found
# DEFAULT_BASE_URL="https://www.runninghub.cn" / DEFAULT_ENDPOINT="…/rhart-video-v3.1-lite-official/text-to-video"
# QUERY_PATH="/openapi/v2/query"（三者均未被改动）
# test_connection 源码：引用 QUERY_PATH=True、引用 self.endpoint=False（探针仍只打查询端点）
```

### 8.8 本轮交付物

| 文件 | 变更 |
|---|---|
| `docs/测试报告-视频Provider替换为RunningHub.md` | **新增本章节 §8**（A/B/C/守卫裁定/回归原始输出） |
| `tests/test_runninghub_adversarial.py` | **新增 5 条**独立证伪用例（`TestEndpointNoDoublePathFalsification` +2、`TestGuardNarrowingSafety` +3）；**既有断言一字未削** |

**未改动**：被测源码（`runninghub_adapter.py` 等）、`config/config.json`、前端静态资源、任何既有测试断言。

### 8.9 Routing Decision（本轮）

**→ NoOne**

- A（双路径）**无法证伪**：12 类填法枚举均无双路径，用户原填法真实提交路径恰为 H3 端点；
- B（H3 参数）**全部通过**，非 H3 端点零污染；
- C（真实接口零成本契约）**实测坐实**：`1001=Invalid URL`、H3 `2K/768P`、默认 `720p/1080p`、H3 `duration∈5..15`；全程未建单、未计费；
- 守卫收窄**裁定正当**（裸 provider 值仍被抓）；
- 全量 **329** 单测 + e2e 56/56 全绿，`config.json` md5 不变；
- 剩余不确定性仅「一次真实付费出片」（§8.6），**非阻塞**。
