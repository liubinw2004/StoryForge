# RunningHub 工作流 API 接入方案（调研 + 落地设计）

> 版本：v1.0　日期：2026-10-08　作者：team-lead
> 状态：调研完成，待 PM/架构/实现阶段承接
> 背景：用户决定移除项目中所有自建图像生成（含服务器已部署的 SD1.5 + IP-Adapter），
> 视频生成改为「RunningHub 工作流 API 的图生视频（i2v）」+「串行首帧链」。

---

## 一、调研结论（接口契约，已核实官方文档与多方样例）

### 1.1 API 形态分类（官方口径）

| 类型 | 用途 | 调用方式 |
|------|------|----------|
| 模型 API | 直接调用图/视频/音频模型 | 提交任务 → taskId → 查询 |
| **AI 应用 API** | 调用**已封装的 AI 应用**，参数贴近业务 | 提交任务 → taskId → 查询 |
| **工作流 API** | 调用多节点编排的工作流（复杂创作） | 提交任务 → taskId → 查询 |
| LLM API | 大模型，兼容 OpenAI/Anthropic/Gemini | 直接请求/流式 |

> **结论**：本需求「图生视频 + 首帧」对应 **AI 应用 API** 或 **工作流 API** 两种形态均可，
> 推荐优先使用 **AI 应用 API**（`/openapi/v2/run/ai-app/{appId}`），因为它是平台对「标准 i2v
> 工作流」的封装，参数更稳定、更贴近「首帧图 + 提示词」这一业务语义；若用户在平台直接用
> ComfyUI 自建工作流，则走 `/openapi/v2/run/workflow/{workflowId}`，两者请求体结构一致（均用 `nodeInfoList`）。

### 1.2 核心接口契约

#### ① 文件上传（首帧图必须先上传）

```
POST https://www.runninghub.cn/openapi/v2/media/upload/binary
Header: Authorization: Bearer ${API_KEY}
Body:   multipart/form-data，字段名 file（本地图片二进制）
```

响应（code=0 成功）：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "type": "image",
    "download_url": "https://...（仅标准模型 API 用）",
    "fileName": "openapi/61432ac1...44.png",
    "size": "3490"
  }
}
```

- **关键字段**：`data.fileName` —— 工作流节点中文件类字段的 `fieldValue` **必须填它**（相对路径）。
- **绝对不能用本地路径、也不能用外链 URL**（该接口不支持传 url）。
- **有效期 24 小时**：`fileName`/`download_url` 一天后失效，任务必须及时完成、产物及时下载。
- 支持类型：图片 JPG/PNG/JPEG/WEBP；ZIP；音频 MP3/WAV/FLAC；视频 MP4/AVI/MOV/MKV。

#### ② 提交 AI 应用任务（标准 i2v 工作流）

```
POST https://www.runninghub.cn/openapi/v2/run/ai-app/{appId}
Header: Authorization: Bearer ${API_KEY}
        Content-Type: application/json
Body:
{
  "nodeInfoList": [
    { "nodeId": "10",   "fieldName": "image",  "fieldValue": "openapi/xxxx.png" },   // 首帧图
    { "nodeId": "17",   "fieldName": "prompt", "fieldValue": "……提示词……" }            // 提示词
  ],
  "instanceType": "default",        // default(24G) | plus(48G) | ultra(84G)
  "usePersonalQueue": false,        // 是否使用个人队列
  "retainSeconds": 180              // 结果保留秒数（10–180），过期结果 URL 失效
}
```

响应（提交成功）：

```json
{ "taskId": "2013508786110730241", "status": "RUNNING", "clientId": "..." }
```

- **`fieldName` 必须是 `fieldValue`**（不是 `value`），这是最容易踩的坑。
- 备选（若走自建 ComfyUI 工作流）：`POST /openapi/v2/run/workflow/{workflowId}`，请求体同上。
- 旧版兼容端点：`POST /task/openapi/ai-app/run`（`apiKey` + `webappId`），新实现不采用。

#### ③ 查询任务结果（轮询）

```
POST https://www.runninghub.cn/openapi/v2/query
Header: Authorization: Bearer ${API_KEY}
        Content-Type: application/json
Body:   { "taskId": "2013508786110730241" }
```

响应：

```json
{
  "taskId": "2013508786110730241",
  "status": "RUNNING",          // QUEUED | RUNNING | SUCCESS | FAILED
  "errorCode": "",
  "errorMessage": "",
  "results": null,              // 成功时： [ { "url": "https://...", "outputType": "mp4", "text": null } ]
  "clientId": "...",
  "promptTips": "...",          // JSON 字符串，节点执行反馈
  "failedReason": null,         // 失败详情
  "usage": { "consumeMoney": "...", "consumeCoins": "...", "taskCostTime": "..." }
}
```

- **成功判定**：`status == "SUCCESS"` 且 `results` 非空；视频产物取 `results[0].url`。
- **产物 URL 24h 内有效，必须立即下载到本地**（这是首帧链“提取尾帧”的前置条件）。

#### ④ 错误码速查

| code | 含义 | 处理 |
|------|------|------|
| 0 | 成功 | — |
| 412 | TOKEN_INVALID（Key 无效/未激活） | 检查 Key（32 位）、会员状态 |
| 421 | 并发/队列超限 | 串行等待后重试（本项目天然串行，规避） |
| 433 | 无效 Key | 检查 Key |
| 434 | 无效 appId/workflowId | 核对 appId |
| 435 | 余额不足 | 充值 |
| 803 | APIKEY_INVALID_NODE_INFO：nodeInfoList 不匹配（节点 ID / 字段名与工作流定义不一致） | 用 getJsonApiFormat 核对 nodeId/fieldName |
| 804 | 任务运行中 | 继续轮询 |
| 805 | 任务失败 | 读 failedReason.traceback，重生成 |
| 810 | WORKFLOW_NOT_SAVED_OR_NOT_RUNNING：工作流未保存或未运行 | 在平台保存并**手动运行一次** |
| 813 | 任务排队中 | 继续轮询 |
| 901 | WEBAPP_NOT_EXISTS：关联的应用 ID 错误 | 核对 appId |

#### ⑤ API Key 类型（影响选型）

| API 类型 | 可用 Key |
|----------|----------|
| 模型 API / LLM API | **仅企业级-共享** |
| **AI 应用 API / 工作流 API** | 消费级-会员、企业级-共享、企业级-独占 |

> **结论**：本方案走 AI 应用 / 工作流 API，**消费级-会员 Key 即可用**（成本最低），
> 无需强制购买企业级独占。基础会员并发通常为 1，恰好契合「串行生成」的设计，避免 421。

### 1.3 关键约束（实现铁律）

1. **文件字段 `fieldValue` 只能是上传返回的 `fileName`**，不能是 URL 或本地路径。
2. **首帧图每次都要重新上传**（或复用一个已上传的 `fileName`，但注意 24h 有效期）。
3. **产物 URL 24h 过期，必须立刻下载**，下载后再用 FFmpeg 提取尾帧。
4. **优先 ASYNC**（同步接口会阻塞 ≥30s，且易超时）；本项目统一走「提交→轮询」异步流。
5. **严格串行**：一次只提交一个任务，轮询到 SUCCESS 后再提交下一个，规避 421。

---

## 二、标准图生视频（i2v）工作流接入方案

### 2.1 单段视频生成的四步流程

```
① 上传首帧图 ──► 拿到 fileName
② 提交 AI 应用任务（nodeInfoList = 首帧 fileName + 提示词）──► 拿到 taskId
③ 轮询 /openapi/v2/query 直到 status=SUCCESS ──► 拿到 results[0].url
④ 下载视频产物到本地 ──► FFmpeg 提取尾帧（供下一段）──► 保存尾帧图片
```

### 2.2 串行首帧链流水线（核心设计）

```
第 1 段：首帧 = 用户手动上传的初始图
        ↓ 生成视频 → 提取尾帧
第 2 段：首帧 = 第 1 段尾帧
        ↓ 生成视频 → 提取尾帧
第 3 段：首帧 = 第 2 段尾帧
        ↓ …
第 N 段：首帧 = 第 N-1 段尾帧
```

- **严格串行**：上一段 SUCCESS 且尾帧提取成功后，才提交下一段，绝不并发。
- **逐段确认**：每段生成完、尾帧展示给用户，用户可「接受并继续」或「重生成本段」。
  - 重生成：重新提交本段（首帧不变、可微调提示词/随机种子），覆盖本段产物。
  - 只有用户「接受」后，才把本段尾帧锁定为下一段首帧，进入下一段。
- **断点/状态**：每段结果（视频路径 + 尾帧路径 + 首帧来源 + taskId）都要持久化，支持中断续跑。

### 2.3 与现有 StoryForge 架构的对接点

| 现有组件 | 处置 |
|----------|------|
| `ImageGenerator` + `image_*` 适配器 + `IMAGE_ADAPTER_REGISTRY` | **整体删除** |
| `EngineSwitcher` / `engine_switch.py` | **整体删除**（不再需要 LLM↔图像切卡） |
| `DirectorAgent` 首帧降级阶梯 L1–L4 | **删除**，改为「串行首帧链」新逻辑 |
| `VideoGenerator` + RunningHub 模型 API 适配器 | **改造**：视频生成改走工作流 i2v，串行执行 |
| 前端「图像生成」配置组 | **删除**；新增「工作流 i2v」配置（appId、首帧上传、逐段确认 UI） |
| `deploy/image_server/`（服务器部署件） | **删除** |
| 服务器 `/workspace/imgserver` + IP-Adapter 权重（≈2.7G） | **远程删除**（保留 SD1.5 基础模型） |
| Phase 2 测试（`test_image_service.py`、stub 图像端点、E2E 图像断言） | **删除/改写** |
| Phase 2 文档（增量 PRD/设计/测试报告） | **归档或删除** |

---

## 三、RunningHub 平台工作流搭建步骤（用户侧操作）

> 本部分由用户在 RunningHub 平台完成，产出 `appId`（或 `workflowId`）与节点参数表，
> 交给实现阶段写入配置。

### 3.1 创建 API Key
1. 登录 [RunningHub](https://www.runninghub.cn)，进入 [API Key 页面](https://www.runninghub.cn/enterprise-api/consumerApi)。
2. 创建 **消费级-会员 Key**（够用）或 **企业级-共享 Key**（更稳）。
3. 复制 Key（32 位字符串），填入 StoryForge 配置（按密钥字段遮罩存储）。

### 3.2 搭建 / 选用标准 i2v 工作流
- 推荐**直接用平台现成的图生视频 AI 应用**（如 MiniMax H3 / Kling / Wan i2v 等），
  避免自建 ComfyUI 工作流的节点调试成本。
- 若需自定义（如绑定具体参考图节点、分辨率、时长），可在「工作流」里自建 ComfyUI 图，
  图内至少含两个输入节点：**LoadImage（首帧图）** + **提示词文本**。

> ⚠️ **关键前置（决定能否被 API 调用）**：工作流必须先在平台「**保存并手动运行一次**」，
> 否则调用 `getJsonApiFormat` 或提交任务都会返回 `810 WORKFLOW_NOT_SAVED_OR_NOT_RUNNING`。

### 3.3 获取 appId / workflowId
- 用 AI 应用 → 记下 **appId**（形如 `2088926295186034689`）。
- 用自建工作流 → 记下 **workflowId**（形如 `1987728214757978114`）。

### 3.4 记录节点参数表（关键，交给实现）
在工作流的「API 调用 / getJsonApiFormat」里查看每个可填节点，记录：

| 字段 | 说明 | 示例 |
|------|------|------|
| nodeId | 首帧图节点的编号 | `10` / `104` / `137` |
| fieldName | 首帧图字段名 | `image` |
| nodeId | 提示词节点的编号 | `17` / `160` / `138` |
| fieldName | 提示词字段名 | `prompt` / `text` / `value` |
| 其它可选项 | 时长、分辨率、seed、negative 等 | 按工作流实际 |

> 实现阶段会把「首帧图节点」「提示词节点」「可选参数」做成**可配置**，
> 用户只要在配置里填对 nodeId/fieldName 即可，无需改代码。

---

## 四、技术决策（team-lead 已裁决，2026-10-09）

1. **接入形态**：**工作流 API**（`POST /openapi/v2/run/workflow/{workflowId}`），workflowId 见 §5。AI 应用 API 留作将来扩展。
2. **尾帧提取**：FFmpeg 截最后一帧，输出 **PNG**。
3. **重生成语义**：允许改提示词、自动换随机种子（nodeId=9 seed）；805 失败自动重试 1 次再交用户。
4. **并发兜底**：应用层全局单任务锁（严格串行），防 421。
5. **配置字段**：`rh_workflow_id`（默认 `2084067455902765057`）、`rh_api_key`（复用 `video_api_key`，遮罩）、
   `rh_instance_type`（default/plus/ultra）、`rh_retain_seconds`（10–180）；首帧图/提示词节点参数默认按 §5 硬编码，可被配置覆盖。

---

## 五、已确认的具体工作流（MiniMax H3 FL2VA 首帧参考生视频）

### 5.1 基本信息
- **名称**：MiniMax H3 FL2VA 开源版 - 首帧参考生视频
- **类型**：ComfyUI 工作流 API（`apiType=5`）
- **workflowId**：`2084067455902765057`
- **提交端点**：`POST https://www.runninghub.cn/openapi/v2/run/workflow/2084067455902765057`
- **已验证**：`getJsonApiFormat` 返回 `code=0`，工作流已发布、可被 API 调用（非 810）。

### 5.2 节点参数表（实测 getJsonApiFormat 拉取）

| 用途 | nodeId | fieldName | 默认值 | 说明 |
|------|--------|-----------|--------|------|
| **首帧图** | `4` | `image` | （示例图 fileName） | `LoadImage`，填上传返回的 `fileName` |
| **提示词** | `7` | `prompt` | （默认运镜文案） | `RHMiniMaxH3FL2VAEncode` |
| 时长（秒） | `6` | `duration_seconds` | `5` | `RHMiniMaxH3FL2VATarget` |
| 画幅 | `6` | `aspect_ratio` | `auto` | 或 `16:9` / `9:16` 等 |
| 随机种子 | `9` | `seed` | `42` | `RHMiniMaxH3DualSigmaSampler` |
| 输出 | `12` | — | mp4 | `SaveVideo`（h264，24fps） |

### 5.3 nodeInfoList 最小示例

```json
{
  "nodeInfoList": [
    { "nodeId": "4", "fieldName": "image",            "fieldValue": "openapi/xxxx.png" },
    { "nodeId": "7", "fieldName": "prompt",           "fieldValue": "one continuous shot, ..." },
    { "nodeId": "6", "fieldName": "duration_seconds", "fieldValue": 5 },
    { "nodeId": "9", "fieldName": "seed",             "fieldValue": 42 }
  ],
  "instanceType": "default"
}
```

### 5.4 相对旧「模型 API」的关键实现差异

1. **上传取 `data.fileName`**（不是 `download_url`）：工作流 `LoadImage` 只认相对路径 fileName。
2. **提交 body 用 `nodeInfoList`**（不是 `{prompt, ratio, resolution, duration}` 平面字段）。
3. **时长 / 画幅 / 种子**改由 `nodeId=6` / `nodeId=9` 覆盖，而非顶层字段。
4. 轮询 `/openapi/v2/query`、下载 `results[0].url`、24h 过期立即下载——**与旧逻辑完全一致，可直接复用**。
