# StoryForge · AI 短剧生成工作台

> 把「一段随便写的对话」变成「一部有分镜、有视频、有评估报告的小短剧」——全程点点鼠标即可完成。

对标腾讯开源项目 [`Tencent/digitalhuman`](https://github.com/Tencent/digitalhuman) 的 **ScriptAgent** 框架，本项目是一个**本地单机 Web 应用**：

```
粗糙对话  →  StoryAgent  →  完整故事情节 + 分镜提示词（story.json）
                          ↓   （可勾选「生成前确认分镜」：人工核对/修改后再继续）
                          ScriptAgent  →  分镜脚本 JSON（script.json）
                          ↓
                          DirectorAgent  →  多镜头视频（逐镜生成 + 视觉连续性）
                          ↓
                          FFmpeg 拼接  →  final_video.mp4
                          ↓
                          CriticAgent  →  评估报告 JSON
```

---

## 快速开始（3 步）

1. 确认本机已安装 **Python 3.10+**（推荐 3.13）与 **FFmpeg**（且已加入 PATH）。
2. 双击项目根目录的 **`start.bat`**。首次运行会自动创建虚拟环境并安装依赖（约 1–3 分钟）。
3. 服务启动后浏览器会自动打开（默认 `http://127.0.0.1:8000`，端口被占用自动 +1）。
   在页面里：配置 → 输入对话 → 点「开始生成」→ 预览下载 → 查看评估报告。

> **零成本跑通**：不配置任何付费 Key，使用默认的「本地 Ollama + 模拟模式（Mock）」即可完整跑完全流程。

---

## 目录结构

```
storyforge/
├── start.bat                 # 一键启动（建 venv → 装依赖 → 检测 FFmpeg → 启动 → 开浏览器）
├── requirements.txt          # Python 依赖声明
├── README.md                 # 本文件
├── docs/
│   ├── PRD.md                # 产品需求文档
│   ├── ARCHITECTURE.md       # 系统架构设计 + 任务分解
│   ├── 使用说明.md            # 面向非专业用户的中文说明书
│   ├── 服务器LLM部署说明.md    # 远程 GPU 服务器 LLM 部署与使用（含性能基准）
│   ├── sequence-diagram.mermaid
│   └── class-diagram.mermaid
├── app/
│   ├── main.py               # FastAPI 入口（路由挂载 / 静态托管 / 端口探测 / 异常兜底）
│   ├── deps.py               # 依赖注入（单例 ConfigStore / TaskManager）
│   ├── models/               # enums.py / schemas.py（Pydantic 模型）
│   ├── routers/              # config_router / pipeline_router / result_router
│   ├── agents/               # base / story / script / director / critic 智能体
│   ├── services/
│   │   ├── llm_client.py     # LLM 双模式（OpenAI 兼容 / Ollama）
│   │   ├── video_generator.py# 视频生成路由
│   │   ├── adapters/         # mock / runninghub 适配器
│   │   ├── task_manager.py   # 内存任务表 + 后台线程 + 状态机
│   │   ├── prompt_templates.py
│   │   ├── json_parser.py    # JSON 容错解析
│   │   └── config_store.py   # 配置持久化（脱敏）
│   ├── utils/                # ffmpeg_utils / env_check / logger / paths / responses
│   └── static/               # index.html / style.css / app.js（零外网依赖）
├── config/config.json        # 运行时生成：用户配置（明文）
├── scripts/
│   ├── e2e_llamacpp.py       # 远程 llama.cpp 端到端自检（写临时配置 → 跑流水线 → 还原）
│   ├── bench_llm.py          # LLM 性能基准
│   └── launcher.py           # start.bat 的启动逻辑
├── outputs/<时间戳>/          # 运行时生成：每次任务的产物
└── logs/app.log              # 运行时生成：应用日志
```

---

## 功能特性

| 能力 | 说明 |
| --- | --- |
| LLM 双模式 | OpenAI 兼容接口（DeepSeek / 通义 / GLM / Kimi / OpenAI…**以及自建的 llama.cpp**）与本地 Ollama 一键切换 |
| 视频适配器 | Mock（P0，FFmpeg 占位视频）/ **RunningHub（模型 API，真实可用：文生视频）** |
| Mock 占位视频 | 渐变色背景 + 镜头编号 `SHOT N/M` + 场景 + 对白 + 底部进度条，1280×720 / 24fps / H.264 |
| 视觉连续性 | 逐镜头抽取上一镜头**末帧**作为下一镜头参考图（`frames/frame_00N.jpg`） |
| 异步执行 | 后台线程跑流水线 + 前端每 1s 轮询，长任务不阻塞、不超时 |
| 剧情补全 | 粗糙对话 → 完整故事情节（`story.json`，含起承转合情节点），再拆解为分镜提示词 |
| 分镜确认（人审门） | 可选「生成前确认分镜」：脚本生成后**挂起**，可视化核对/修改**完整剧情**与**逐镜头分镜提示词**，确认后**原地续跑**（改剧情则重跑脚本，仅改分镜则不重跑） |
| 专业时间轴剪辑 | 可视化时间轴：拖拽排序、**双臂拖拽裁剪**、播放头定位、**一刀两段（split）**、片段启停；宽度正比时长，方案自动保存、刷新自动恢复 |
| 三套状态机 | 任务级 / 阶段级 / 镜头级（pending|waiting → running → success\|failed；另有非终态 `awaiting_review`） |
| 容错 | 单镜头失败不阻断流程，可单独重试；全部镜头失败才置任务失败 |
| JSON 容错 | 代码块提取 → 最大括号匹配 → 去尾随逗号/修引号 → 失败自动重试（最多 2 次） |
| 统一响应 | `{ ok, data, error }`；全局异常兜底，不泄漏 traceback |
| 安全 | api_key 返回脱敏 `sk-****abcd`；文件名参数做路径穿越校验；只暴露相对路径 |

---

## 配置说明

配置保存在 `config/config.json`（明文，本地单机应用风险可接受），也可直接在页面「配置区」修改并保存（页面读取时脱敏）。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `llm_mode` | `ollama` | `ollama`（零成本）/ `openai`（OpenAI 兼容） |
| `llm_base_url` | `http://localhost:11434` | 切 OpenAI 时填 `https://api.deepseek.com/v1`；用远程 llama.cpp 时填 `http://127.0.0.1:8080/v1` |
| `llm_model` | `qwen2.5:14b` | 模型名（远程 GPU 服务器 llama.cpp / Ollama 均填 `qwen3:30b-a3b`，Ollama 亦可填 `qwen2.5:14b`） |
| `llm_api_key` | `""` | Ollama 模式忽略；llama.cpp 模式填任意非空占位（如 `sk-local-llamacpp`） |
| `video_provider` | `mock` | `mock` / `runninghub` |
| `video_api_key` | `""` | 真实视频 API 的密钥（选 RunningHub 时必填，需「企业级-共享」Key） |
| `video_base_url` | `""` | 留空用国内站 `https://www.runninghub.cn`（国际站 `https://www.runninghub.ai`）；**也可直接粘贴完整模型端点 URL**（如 `https://www.runninghub.cn/openapi/v2/<模型>/text-to-video`），程序会自动拆分主机与路径、**不重复拼接** |
| `video_model` | `""` | 端点**路径**（如 `openapi/v2/<模型>/text-to-video`）或完整 URL；留空用默认端点 `/openapi/v2/rhart-video-v3.1-lite-official/text-to-video` |
| `consistency_lock` | `true` | 提示词锁定卡：角色外观卡 + 场景卡逐字注入**每个**镜头（零成本，一致性主力） |
| `chain_continuity` | `true` | 同场景内用**上一镜末帧**作下一镜首帧参考（**仅 H3 家族端点支持**；不支持时自动降级并只记一条提示） |
| `anchor_image` | `true` | 每场景先用**文生图**生成 16:9 开场帧作链首（⚠️ **额外文生图费用**；取值无法识别时一律按 `false` 处理） |
| `video_image_endpoint` | `""` | 文生图端点，留空用默认 `rhart-image-n-pro-official/text-to-image`；也可填完整 URL 覆盖 |
| `resolution` | `720p` | `720p` / `1080p` |
| `style` | `realistic` | `realistic` / `anime` / `animation` |
| `shot_count` | `4` | 2–8 |
| `shot_duration` | `4` | 2–10（秒） |
| `skip_video_eval` | `false` | 勾选后仅评估脚本 |

---

## 使用远程 GPU 服务器 LLM（推荐）

本机不装大模型也能用——项目已在一台**远程 GPU 服务器**（2× NVIDIA TITAN Xp，Pascal）上
用 **llama.cpp** 部署好 `qwen3:30b-a3b`（MoE，实测 **≈ 84 tokens/s**），
通过一条 **SSH 隧道**即可使用。同一份模型上也保留了 **Ollama** 作为备选（见部署说明附录 B）。

**仅需 3 步：**

```bash
# 1) 建立 SSH 隧道（保持该窗口不关；加 -f 可后台运行）
ssh -N -L 8080:127.0.0.1:8080 -p 29902 ubuntu@hpc.vmip.com.cn
```

```
# 2) 在 StoryForge「配置区」填写（注意：llama.cpp 必须用 OpenAI 兼容模式！）：
#    LLM 模式 = OpenAI 兼容      ← 不要选「本地 Ollama」
#    Base URL = http://127.0.0.1:8080/v1
#    模型名   = qwen3-30b-a3b
#    API Key  = sk-local-llamacpp   （任意非空占位即可）
```

```
# 3) 点「测试 LLM」，提示「连接成功，检测到 1 个模型」即成功。
```

> ⚠️ **llama.cpp 没有 Ollama 的 `/api/tags` 接口**，所以「本地 Ollama」模式会 404，必须用
> 「OpenAI 兼容」模式（Base URL 结尾要带 `/v1`）。
>
> **为什么选 llama.cpp**：它在**服务端**彻底关闭了 Qwen3 Thinking 版的思考模式，
> `content` 一定有正文；同一套 StoryForge 流水线实测 **≈ 19 秒**（Ollama 约 103 秒，快 **≈ 5.43 倍**）。
>
> ⚠️ 该模型是 **Thinking 微调版**：**请保留强 system 约束并明确要求 JSON 输出**
> （项目内置提示词已满足），否则直接裸调可能得到"散文"而非 JSON。详见部署说明第 7.4 节。
>
> 服务器上的服务只监听其本机 `127.0.0.1:8080`，必须靠 SSH 隧道接入，**不要**直连新端口。
> 完整说明（一键使用、架构、性能基准对比、思考模式原理、排错、Ollama 备选）见
> **[`docs/服务器LLM部署说明.md`](docs/服务器LLM部署说明.md)**。

一键端到端自检（会自动写临时配置、跑完整流水线、再还原配置）：

```bash
.venv\Scripts\python.exe scripts\e2e_llamacpp.py
```

---

## 用 RunningHub（模型 API）生成真实视频

除零成本的 Mock 外，本版本**已真实接通 RunningHub 模型 API**（`VideoProvider.RUNNINGHUB`）。
在「配置区 → 视频生成」选择 **RunningHub（模型 API）**，填写：

| 字段 | 填写规则 |
| --- | --- |
| `video_api_key` | **必填**，RunningHub 控制台申请的 API Key（⚠️ **模型 API 仅支持「企业级-共享」Key**） |
| `video_base_url` | **留空即可**（默认国内站 `https://www.runninghub.cn`；国际站填 `https://www.runninghub.ai`）。**换模型时可直接把完整模型端点 URL 粘在这里**——程序自动拆分主机与路径，不会重复拼接 |
| `video_model` | **留空即可**（默认端点路径 `/openapi/v2/rhart-video-v3.1-lite-official/text-to-video`）。也可填其它模型端点路径或完整 URL |

> **参数自动适配**：不同模型端点字段名/枚举不同，程序按端点路径自动切换 ——
> 默认端点用 `aspectRatio` + `720p/1080p` + `4/6/8` 秒；`…/hailuo-h3/…` 端点用 `ratio` + `768P/2K` + `5–15` 秒。

#### 人物与场景一致性（三条腿）

跨分镜「人不一样、景不一样」由三个开关协同解决（默认全开）：

| 机制 | 开关 | 作用 |
| --- | --- | --- |
| **提示词锁定卡** | `consistency_lock` | 分镜阶段由 LLM 产出英文「角色外观卡 / 场景卡」，拼成 `风格前缀 · SCENE: … · CHARACTERS: 名字=外观卡 · SHOT: …` 逐镜注入 |
| **场景内链式衔接** | `chain_continuity` | 同场景内把上一镜末帧作为下一镜 `firstFrameUrl`（图生视频） |
| **每场景开场帧** | `anchor_image` | 换场景时先用文生图生成一张 16:9 链首图；产物 `frames/scene_XX.jpg` **存在即复用**（不重复收费） |

- 端点不支持 i2v（如默认 `v3.1-lite`）→ 链式**自动关闭**，日志只记**一条**降级提示；
- `anchor_image` 取值无法识别（`null` / `[]` / `"maybe"`…）→ 一律按 **`false`** 处理（成本安全，不替你打开付费开关）；
- 重试单镜时同样遵守 `chain_continuity` 总开关（关闭则按磁盘重建的参考帧也不传入）。

点「测试视频接口」会做一次**零成本探针**（仅 `POST {base}/openapi/v2/query`，body `{"taskId":"0"}`，**绝不创建真实生成任务**）：
未填 Key → 「未配置…」；端点 4xx/5xx → 「端点返回 HTTP …，请检查 Base URL」；鉴权失败（`errorCode`/`errorMessage` 命中 `key`/`401`/`403`/`433`）→ 「API Key 无效或未授权」；否则 → 「端点可达、鉴权通过」。

**调用链路**（`POST` 提交 + `POST` 查询，`Authorization: Bearer`）：

```
POST {base}{endpoint}         → {"taskId": "...", "status": "RUNNING", ...}
POST {base}/openapi/v2/query  → QUEUED → RUNNING → SUCCESS | FAILED
                                 SUCCESS 时 results[0].url 直接给出下载地址（COS 临时链接，约 24h 过期）
GET  results[0].url           → 落盘 shots/shot_00N.mp4（与 Mock 同一契约，下游 FFmpeg 无差异）
```

**请求体**（扁平参数，注意 `duration` 为**字符串**）：

```json
{
  "prompt": "<分镜提示词>",
  "aspectRatio": "16:9",
  "resolution": "720p",
  "duration": "6"
}
```

**规格与约定**：

- **时长枚举仅 `4` / `6` / `8`**：超出范围或取中间值自动**就近钳制**（如 `2→4`、`5→4`、`7→6`、`10→8`，平局取下限）并记日志。
- **`aspectRatio`**：由画面尺寸派生（宽 ≥ 高 → `16:9`，竖屏 → `9:16`）。
- **分辨率**：`720p` / `1080p`（取自配置）。
- **参考图**：模型 API 的文生视频端点**不接受首帧参考图** → **忽略并记 warn**（不静默、不报错）；如需视觉连续性请改用 RunningHub 的 **AI 应用 / 工作流** API。
- **轮询**：每 **5s** 一次，最长 **600s**；超时或 `status=FAILED`（或**非空 `errorCode`**）→ `VID_GENERATE_FAILED`（message 均带官方 `errorCode` / `errorMessage`）。
- **错误码**：未配置 → `VID_NOT_CONFIGURED`；鉴权/接口错误 → `VID_API_ERROR`；生成失败/超时 → `VID_GENERATE_FAILED`。
- 零新增第三方依赖：仅用项目既有的 `httpx`。

> 单测 `tests/test_runninghub_adapter.py` 使用**本地桩 HTTP 服务**完整模拟「提交 → 轮询 → 下载」链路 + 落盘，**不访问真实外网**。

---

## 手动启动（不使用 start.bat）

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 启动服务（端口 8000，被占用自动 +1，浏览器自动打开）
.venv\Scripts\python.exe -m app.main
```

也可使用 Uvicorn 直接启动（此时端口固定、不自动开浏览器）：

```bash
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

---

## API 一览

所有响应统一为 `{ "ok": bool, "data": any, "error": str|null }`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 启动自检（端口 / FFmpeg / Python） |
| GET | `/api/config` | 读取配置（脱敏） |
| POST | `/api/config` | 保存配置（脱敏值不覆盖原值） |
| POST | `/api/config/test-llm` | 测试 LLM 连通性 |
| POST | `/api/config/test-video` | 测试视频接口 |
| GET | `/api/config/ollama-models` | 探测本地 Ollama 模型 |
| POST | `/api/pipeline/run` | 启动流水线（立即返回 `task_id`；可选 `review=true` 开启生成前确认） |
| GET | `/api/pipeline/{id}/status` | 轮询任务状态（含非终态 `awaiting_review`） |
| GET | `/api/pipeline/{id}/story` | 获取补全后的剧情 JSON（StoryDocument） |
| GET | `/api/pipeline/{id}/script` | 获取脚本 JSON |
| GET | `/api/pipeline/{id}/report` | 获取评估报告 JSON |
| POST | `/api/pipeline/{id}/confirm-script` | 确认分镜并**原地续跑**（人审门；可携带编辑后的 `story`/`script`） |
| POST | `/api/pipeline/{id}/retry-shot` | 重试单个镜头 |
| POST | `/api/pipeline/{id}/export` | 按剪辑方案（EDL）导出新成片 + 配对命名方案（不覆盖 `final_video.mp4`） |
| GET | `/api/pipeline/{id}/edit-plan` | 读取剪辑方案草稿（刷新恢复用） |
| PUT | `/api/pipeline/{id}/edit-plan` | 保存剪辑方案草稿（仅写 JSON，不触发 FFmpeg） |
| GET | `/api/pipeline/{id}/video/{name}` | 播放视频（`final_video.mp4` / `shots/shot_00N.mp4`） |
| GET | `/api/pipeline/{id}/frames/{name}` | 预览末帧图片 |
| GET | `/api/pipeline/{id}/download` | 下载成片 |
| GET | `/api/pipeline/{id}/download-artifact?name=` | 下载中间产物 |

---

## 技术栈

- **后端**：Python 3.13 + FastAPI + Uvicorn + Pydantic v2 + httpx + openai SDK
- **前端**：原生 HTML + 自写 CSS + 原生 JS，**零外部 CDN 依赖、无构建链**
- **媒体处理**：直接调用 FFmpeg 命令行（不引入 moviepy）

---

## 常见问题

- **FFmpeg 缺失** → 页面顶部红色横幅提示；请安装并加入 PATH 后重启服务。
- **Ollama 未启动** → 提示「无法连接 localhost:11434」；请先执行 `ollama serve`，或手动填写 Base URL 与模型名。
- **JSON 解析失败** → 部分小模型输出不稳定，系统会自动重试 2 次；仍失败请更换更大的模型。
- **提示 `LLM_CONTENT_EMPTY`（思考模式未产出正文）** → 使用了思考型模型（如 Qwen3 Thinking 版）且额度被思考内容吃光。
  「本地 Ollama」模式会自动带上 `think: false`；若走**远程 llama.cpp**，其启动脚本已在服务端关闭思考，正常不会出现。
  代码侧也会给出明确中文提示而非笼统"空内容"（未显式指定时 `max_tokens` 默认 4096）。
  详见 [`docs/服务器LLM部署说明.md`](docs/服务器LLM部署说明.md) 第 7 节与第 9 节。
- **远程 llama.cpp 报 `Ollama 返回异常状态码` / 404** → 误用了「本地 Ollama」模式。llama.cpp 无 `/api/tags`，
  请把「LLM 模式」改成「OpenAI 兼容」，Base URL 填 `http://127.0.0.1:8080/v1`。
- **端口被占用** → 服务自动使用 8001、8002……，页面顶部显示实际地址。

更多细节见 [`docs/使用说明.md`](docs/使用说明.md)。

---

## 许可

本项目仅用于学习与研究，参考腾讯开源项目 `Tencent/digitalhuman` 的 ScriptAgent 设计理念。
