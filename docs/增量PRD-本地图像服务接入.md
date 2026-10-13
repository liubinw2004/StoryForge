# 增量 PRD：本地图像服务接入（Phase 2）

> 文档类型：**增量 PRD**（只描述本次变更，不重复旧功能）
> 项目：StoryForge（Python 3.13 + FastAPI + 原生 HTML/CSS/JS，零 CDN）
> 版本：v0.3 增量　|　语言：简体中文　|　作者：许清楚（PM）
> 事实来源：`deploy/image_server/README.md`（已实测通过，直接引用，不再调研）

---

## 1. 背景与目标

**背景（一句话）**：现有三条一致性「腿」中的 `anchor_image` 依赖 RunningHub **纯文生图**，吃不了参考图 → **锁不住同一张脸**；逐镜之间又只有「上一镜末帧」一条弱约束。服务器上已实测通过一套**本地 SD1.5 + IP-Adapter 图像服务**（可吃参考图、可固定 seed、1280x720 单张 ≈6.5s、零 API 费用），本轮把它接进 StoryForge，把一致性升级为「**定妆照 → 逐镜首帧 → H3 i2v**」。

**目标（≤3 条）**
1. 让「同一个角色」在整片里**看起来是同一个人**：每角色一张固定 seed 定妆照，逐镜首帧以定妆照为参考图。
2. **默认零成本**：图像默认走本机 8090 本地服务；任何「花钱」能力默认关闭。
3. **默认安全**：图像阶段前后由应用**自动 ssh 切换** LLM/图像服务；任何失败**降级而非中断**，绝不静默回退到收费文生图。

---

## 2. 用户故事

| # | As a | I want | So that |
|---|---|---|---|
| US1 | 短剧创作者 | 用本机图像服务免费出「定妆照 + 逐镜首帧」 | 在不增加 API 花费的前提下把人物锁住 |
| US2 | 短剧创作者 | 应用自己完成「停 LLM / 起图像服务 / 出图 / 切回 LLM」 | 不必手工敲 `start.sh`/`stop.sh` |
| US3 | 短剧创作者 | 图像服务不可达时任务照常出片（只是首帧降级） | 不被网络/隧道问题卡死整条流水线 |
| US4 | 短剧创作者 | 换一台机器/换一个端点也能跑（端点可配） | 不必改代码，只改配置 |
| US5 | 短剧创作者 | 同一 seed 重跑得到同样的定妆照，且已生成的直接复用 | 省钱、省时、画面稳定 |
| US6 | 进阶用户 | 想花钱时才显式切到 RunningHub 出图 | 成本可控，不会被默认偷偷计费 |

---

## 3. 需求池（P0 必做 / P1 应做 / P2 可选）

### 3.1 P0（Must have，本次交付）
| 编号 | 需求 | 覆盖 AC |
|---|---|---|
| R1 | **图像提供方配置**：新增 `image_provider`（`mock`\|`local`\|`runninghub`，默认 **`local`**）、`image_endpoint`（默认 `http://127.0.0.1:8090/v1/images/generations`）、`image_api_key`（可选）、`ip_scale`（默认 ≈0.4）、`ip_face`（默认 `true`）、`auto_engine_switch`（默认 `true`）。字段名最终由架构师定，本 PRD 只约束**产品语义与默认值** | AC1, AC2, AC19, AC21 |
| R2 | **参考图与调参透传**：图像生成能力支持 `ref_image`（本地路径/data URI/裸 base64）+ `ip_scale`/`ip_face`/`seed`，按 `README §5` 契约发给 8090 | AC3 |
| R3 | **角色定妆照**：为脚本中每个角色生成一张定妆照（**固定 seed**），落盘 `frames/character_<name>.jpg`；**已存在且非空则复用** | AC4, AC18 |
| R4 | **逐镜首帧**：每个镜头生成 `frames/shot_first_{i:03d}.jpg` =「角色定妆照作参考图 + 场景锁定卡 + 该镜画面提示词」，并作为 H3 i2v 的 `firstFrameUrl`（**取代旧的「上一镜末帧」作首帧**） | AC5, AC6, AC11 |
| R5 | **引擎切换编排**：图像阶段前应用自动 `ssh … start.sh`（停 LLM / 起图像服务），阶段后自动 `ssh … stop.sh --with-llm`（恢复 LLM），用户无需手敲命令 | AC8, AC9, AC12 |
| R6 | **降级与安全**：不可达/切换失败/出图失败一律降级，任务不中断（详见 §5）；**绝不**静默回退 RunningHub 收费文生图 | AC7, AC10 |
| R7 | **铁律合规**：零 CDN、零新增 pip、任务相对正斜杠路径、`StageName.ALL` 恒 5、`.bat` 纯 ASCII+CRLF | AC13, AC14, AC15, AC16, AC17 |
| R8 | **端到端回归 + 文档**：`docs/使用说明.md` 增补使用与降级说明 | AC20 |

### 3.2 P1（Should have）
| 编号 | 需求 |
|---|---|
| P1-1 | 配置区「**测试图像服务**」按钮：探活 `GET /health`，返回 `ready/busy/gpu/ip_adapter` 状态（失败仅提示，不阻塞） |
| P1-2 | 结果区展示**逐镜首帧缩略图**（复用现有 frames 展示样式），便于肉眼核对一致性 |
| P1-3 | 「**仅生成定妆照**」独立入口：便于换脸/调参时快速预览与**替换** |

### 3.3 P2（Nice to have）
| 编号 | 需求 |
|---|---|
| P2-1 | `ip_scale` 以**滑块**（0.2–0.5）暴露，UI 标注「≥0.6 会崩脸，勿用」 |
| P2-2 | 定妆照支持**人工上传替换**（不重算） |
| P2-3 | 逐镜首帧 **A/B 对照**（同 seed 开/关 `ref_image`），用于效果评估 |

---

## 4. 验收标准（Given / When / Then）

| 编号 | Given | When | Then |
|---|---|---|---|
| AC1 | 保存含 `image_provider=local` 的配置 | `GET /api/config` | 回显该字段；`image_api_key` 为脱敏值 |
| AC2 | 磁盘配置缺 `image_provider` | 加载配置 | 默认 `local`；RunningHub 出图路径**默认不启用** |
| AC3 | 已配置 `local` 且服务就绪 | 生成任一帧 | 发往 8090 的请求体含 `ref_image`/`ip_scale`/`ip_face`/`seed`，字段名与 README §5 一致 |
| AC4 | 脚本含 2 个角色 | director 完成 | `frames/character_<name>.jpg` 各存在 1 张；同 seed 重跑文件哈希不变 |
| AC5 | 脚本含 N 镜 | director 完成 | 生成 `frames/shot_first_001.jpg…shot_first_{N:03d}.jpg`，编号连续 |
| AC6 | 视频走 RunningHub H3 i2v | 提交镜头 | `firstFrameUrl` 指向该镜 **shot_first** 图（非上一镜末帧） |
| AC7 | 8090 不可达（隧道断/服务未起） | 跑完整流水线 | 任务仍 `success`；镜头回落**无首帧文生视频**；日志含降级说明；**未发起任何 RunningHub 文生图请求** |
| AC8 | `start.sh` 返回非 0 或超过硬超时（建议 ≤90s） | 进入图像阶段 | **不阻塞**流水线，按 AC7 降级 |
| AC9 | 图像阶段已结束 | 收尾 | 调用 `stop.sh --with-llm`；若失败，前端**显式提示**「请手动执行 stop.sh --with-llm」，不静默 |
| AC10 | 某镜首帧出图失败（5xx/超时） | 继续后续镜头 | 仅该镜降级，其余镜头照常产出；`ShotResult.error` 有记录 |
| AC11 | 脚本 `characters` 为空 | 生成逐镜首帧 | 跳过定妆照；首帧仍产出（**不传** `ref_image`，纯文生图） |
| AC12 | `auto_engine_switch=true` | 观察切换时机 | 切换仅发生在图像阶段前后；文档说明切换期间 LLM 暂不可用 |
| AC13 | 打开 `app/static/index.html` 检查 | 全局检索 | **无任何 `http(s)://` 外链资源**（零 CDN） |
| AC14 | 新增图像服务接入代码 | 依赖审计 | **未新增任何 pip 包**；HTTP 用既有 `httpx`；FFmpeg 用命令行子进程 |
| AC15 | 任一对外字段（如首帧、定妆照路径） | 检查 API 响应 | 均为任务相对正斜杠路径（如 `frames/shot_first_001.jpg`），无 `E:\…` 绝对路径 |
| AC16 | 检查阶段定义 | 运行 | `StageName.ALL` 恒为 5（`story→script→director→concat→critic`），前端 `data-stage` 锁死，**未新增阶段** |
| AC17 | 新增/修改的 `.bat` 文件 | 检查字节 | 纯 ASCII + CRLF |
| AC18 | 定妆照/首帧已存在且非空 | 重跑 | 直接复用，不重复出图 |
| AC19 | 默认配置 | 检查端点 | `image_endpoint` 默认 `http://127.0.0.1:8090/v1/images/generations`，经 SSH 隧道访问 |
| AC20 | 交付完成 | 查阅 `docs/使用说明.md` | 新增「本地图像服务（省钱 / 换机 / 失败降级）」章节 |
| AC21 | 打开配置区 | 观察 | 新增「图像提供方」单选（`mock`/`local`/`runninghub`，默认 `local`）+ `ip_scale`/`ip_face` 控件，文案说明「本地零成本、GPU 与 LLM 互斥」 |

---

## 5. UI 变更点（仅配置区新增，其余不变）

| 位置 | 控件 | 默认值 | 文案要点 |
|---|---|---|---|
| ① 配置区 · 新增「图像生成」分组 | 单选：图像提供方 `mock`/`local`/`runninghub` | **`local`** | 「本地图像服务（零成本，需开隧道 8090）」/「RunningHub 出图（**会产生费用**）」/「模拟（不出图）」 |
| 同上 | 文本：图像端点 | `http://127.0.0.1:8090/v1/images/generations` | 换机器只改这一行 |
| 同上 | 复选框：自动引擎切换（ssh） | **勾选** | 「自动停 LLM / 起图像服务 / 用后切回；切换期间对话功能暂不可用」 |
| 同上 | 数字/滑块：`ip_scale`（P2 升级为滑块） | `0.4` | 「越大越像参考图，但越不听提示词；≥0.6 会崩脸，勿用」 |
| 同上 | 复选框：`ip_face`（人脸专用适配器） | **勾选** | 「人脸更干净，推荐开启」 |
| 同上 | 数字：`image_seed`（固定种子） | `20261007` | 「定妆照与逐镜首帧共用；固定 seed 才能缓存复用、重试得同图」 |
| 同上 | 复选框：`shot_first_frame`（**逐镜首帧，主路径**） | **勾选** | 「每镜生成首帧作 i2v 首帧（走图像提供方），真正锁人物与场景」 |
| 同上 | 复选框：`anchor_image`（每场景锚图，兜底位） | **勾选** | 「逐镜首帧失败时的**降级兜底**（仅场景首镜）；走**图像提供方**出图」 |
| 同上 | 文本：引擎切换 start 命令（P1 可折叠） | 空 | placeholder 示例 `ssh -p 29902 ubuntu@hpc.vmip.com.cn 'bash /workspace/imgserver/start.sh'`；**留空 = 不切** |
| 同上 | 文本：引擎切换 stop 命令（P1 可折叠） | 空 | placeholder 示例 `… 'bash /workspace/imgserver/stop.sh --with-llm'`；**留空 = 不切** |
| 同上 | 按钮：测试图像服务（P1） | — | 显示 `ready/busy/gpu`，失败仅提示 |

> **新旧开关关系（见 §9 裁定 5，并已被架构设计 §1.4 进一步定稿）**：`shot_first_frame`（默认 `true`，主路径）与 `anchor_image`（默认 `true`，兜底位）**同时为真时只走逐镜首帧，绝不双份出图**；`anchor_image` 作为逐镜首帧**失败后的降级兜底**（仅场景首镜），**所有静态图一律走 `image_provider`**。`shot_first_frame=false` 时可复现旧行为。

---

## 6. 失败模式与降级策略（**必须逐条覆盖**）

| 失败模式 | 处理策略 | 是否中断 |
|---|---|---|
| 图像服务**不可达**（隧道断/8090 无响应） | 跳过定妆照与逐镜首帧；镜头回落为**无首帧的纯文生视频**（沿用 `video_provider`）；日志 + 前端明确提示「图像服务不可达，已降级」；**不自动切 RunningHub 出图** | 否 |
| **ssh 切换失败**（`start.sh` 非 0 / 超时） | 设硬超时（建议 ≤90s），超时即按「图像服务不可达」降级，**绝不阻塞** | 否 |
| `stop.sh --with-llm` **切不回 LLM** | 图像阶段正常结束；阶段边界**重试一次**；仍失败 → 前端持续提示「服务器 LLM 未恢复，请手动执行 `stop.sh --with-llm`」 | 否（但强提示） |
| **单镜首帧出图失败**（5xx/超时） | 仅该镜降级为无首帧文生视频，其余镜头继续；记 `ShotResult.error` | 否 |
| 分镜**无角色**（`characters` 为空） | 跳过定妆照；逐镜首帧改用「场景卡 + 镜头提示词」纯文生图（**不传** `ref_image`） | 否 |
| **定妆照出图失败** | 该角色无参考图；其所在镜头首帧降级为纯文生图；不影响其他角色 | 否 |
| **场景列表为空**（`scenes` 空） | 全部镜头按单场景（`script.scene`）处理 | 否 |
| 图像服务 `busy`（上一次未结束） | 串行等待或跳过本次；不并发压垮单卡 | 否 |

---

## 7. 成本与安全边界

- **零 API 费用（默认路径）**：`image_provider=local` 的定妆照与逐镜首帧 = 服务器本地 GPU 出图，**仅耗电 + 暂占服务器 GPU**，无按次计费。
- **会产生费用的操作（默认均**不**启用或需显式选择）**：
  1. `image_provider=runninghub` 的 RunningHub **文生图**（默认关）；
  2. `video_provider=runninghub` 的 RunningHub **i2v / 文生视频**（既有能力，按次计费）。
- **默认开关状态**：`image_provider=local`；`shot_first_frame=true`（主路径）、`anchor_image=true`（回退位，因优先级规则默认只走免费的逐镜首帧）；`auto_engine_switch=true`（仅触发本机→服务器 ssh 脚本，**无 API 费用**）；`image_switch_start_cmd` / `image_switch_stop_cmd` 默认**空串**（空 = 不切）；`runninghub` 图像路径默认不启用。
- **安全红线**：图像服务失败**绝不静默回退**到 RunningHub 收费文生图；**不动 `config/config.json`**（含用户真实 Key，任何自动化流程不得改写它；E2E 临时改写并还原属既有机制）；服务器 GPU 与 LLM **硬互斥**，故切换期间 LLM 不可用须在 UI 明示。

---

## 8. 待确认问题

**无。** 原 4 条待确认项已全部由 team-lead 裁定，见 **§9 决策确认**。

---

## 9. 决策确认（team-lead 裁定）

> 以下均为**已定稿**决策，架构师据此设计实现，不再二次确认。

| 编号 | 决策 | 结论与具体口径 |
|---|---|---|
| 裁定 1 | **引擎切换粒度** | **整条流水线一次切换**，不做逐镜临时切换（切回 LLM 需重启 llama.cpp、冷加载 18GB MoE 约 2–3 分钟，频繁切换代价远大于收益）。窗口：**进入 `director` 阶段前**一次性「停 LLM / 起图像服务」；**`director` 阶段全部出图结束后**一次性「停图像服务 / 起 LLM」。**例外**：**单镜重试（retry-shot）** 亦为一次独立切换窗口（进入前确保图像引擎在、结束 `finally` 切回）。两处均须 `try/finally` 保证切回。 |
| 裁定 2 | **i2v 与出图** | **保持串行，不做并行**。单卡 + 图像服务为 `busy` 串行模型，且同一镜内本就强依赖（先有首帧才能提交 i2v）；并行只增复杂度、收益有限。 |
| 裁定 3 | **seed** | 定妆照与逐镜首帧**共用同一个固定常量 seed**（默认 `20261007`），并作可配置项暴露（字段名由架构师定，建议 `image_seed`）。理由：可复现 + 缓存幂等 + 重试得同图；固定 seed 不会让不同角色撞图（prompt 不同）。 |
| 裁定 4 | **P1-3「仅生成定妆照」入口** | **不进 P0，保留 P1**。本轮 P0 已有 4 件重活（定妆照 / 逐镜首帧 / 自动切换 / 降级），配置区改动已较大，该入口属便利功能。 |
| 裁定 5 | **新旧开关关系** | 新增 `shot_first_frame`（逐镜首帧，**主路径**，走 `image_provider`），默认 `true`；原 `anchor_image`（每场景锚图）**保留为降级兜底位**。**（实现口径已按架构设计 §1.4 定稿更新）**：`anchor_image` 作逐镜首帧失败后的 **L2 降级兜底**（仅场景首镜），且**所有静态图（定妆照/逐镜首帧/场景锚图）一律经 `ImageGenerator`、由 `image_provider` 决定来源**（非 `video_provider`）；两者同时为真只走逐镜首帧，**绝不双份出图**；`shot_first_frame=false` 时可完全复现旧行为。 |
| 裁定 6 | **引擎切换配置默认值** | `auto_engine_switch` 默认 `true`（UI 默认勾选）；配套两条**默认空字符串**的命令配置 `image_switch_start_cmd` / `image_switch_stop_cmd`（示例值在 UI placeholder 与文档给出）。**空命令 = 不做任何 ssh**，只记一条 info「未配置引擎切换命令，跳过自动切换」，然后直接尝试连接图像端点（既安全又不噪声）。**非空时**：先探活 `GET /health`，**已 `ready` 则跳过 start**（幂等，避免重复切换），否则执行 start；结束 `finally` 执行 stop；两命令均有**硬超时（≤90s，超时即降级）**。 |

**对 PRD 正文的同步修订**：§5 配置区新增 `image_seed` / `shot_first_frame` / `anchor_image` / 两条切换命令控件，并明确新旧开关关系；§7 默认开关状态同步；§8 由 4 条待确认改为「无」。
