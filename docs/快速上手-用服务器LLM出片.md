# StoryForge 快速上手 · 用 GPU 服务器上的 LLM 出片

> 目标：在本机浏览器里，用远程 GPU 服务器（2×TITAN Xp）上的 `qwen3-30b-a3b` 当大脑，
> 走完「对话 → 脚本 → 分镜视频 → 拼接 → 评分」全流程。
>
> 最近一次验证：2026-10-04，端到端跑通，脚本评分 8.3 / 视频评分 8.2。

---

## 0. 先看清这条链路

```
浏览器 (StoryForge UI)
        │  http://127.0.0.1:8000
        ▼
本机 StoryForge 后端 (FastAPI)
        │  http://127.0.0.1:8080/v1   ← OpenAI 兼容协议
        ▼
本机 SSH 隧道端口中转  【必须常开！】
        │  ssh -L 8080:127.0.0.1:8080 -p 29902
        ▼
服务器 hpc.vmip.com.cn
        │  服务器内部 http://127.0.0.1:8080
        ▼
llama.cpp llama-server  →  qwen3-30b-a3b.gguf (18 GB MoE)
```

**两个窗口都得开着**：① 隧道窗口 ② StoryForge 启动窗口。关掉任意一个都会失败。

---

## ⚠️ 先读这段（最重要）

| 事实 | 说明 |
|---|---|
| **当前只能出「占位视频」** | 视频生成默认且唯一可用的是 **Mock 模式**：FFmpeg 生成的渐变底色 + 镜头文字 + 进度条，**不是真实画面**。它是用来验证整条流水线跑通的。 |
| **想真出片就接真实视频 API** | 除零成本的 **Mock 模式**外，本版本**已内置 RunningHub 模型 API 适配器**（文生视频，需「企业级-共享」Key）。在配置区选「RunningHub（模型 API）」填 Key 即可真实出片，**无需改任何代码**。 |
| **LLM 必须选「OpenAI 兼容接口」** | 千万别选「本地 Ollama（零成本）」——服务器上跑的是 llama.cpp，不是 Ollama，选错会 404。 |
| **API Key 随便填但不能空** | llama-server 没设鉴权，但 StoryForge 走标准 OpenAI 客户端，Key 为空会被拦。填 `sk-local-llamacpp` 这种占位串即可。 |
| **一次只跑一路** | `qwen3-30b-a3b` 单请求就吃约 22 GB 显存（两张 12 GB 卡合计约 24 GB），同时只能服务 1 路大请求。别同时开两个生成任务。 |

---

## 第 1 步 · 打开 GPU 服务器上的 LLM 服务

先确认服务在不在（在你自己的电脑上执行）：

```bash
ssh -p 29902 ubuntu@hpc.vmip.com.cn 'curl -s --max-time 5 http://127.0.0.1:8080/v1/models'
```

- 返回 `{"models":[{"name":"qwen3-30b-a3b", ...}]}` → **已就绪，跳到第 2 步**。
- 返回 `503 Loading model` → **正在加载，等 1~3 分钟再看**。
- 无任何输出 / 连接失败 → 服务没跑，启动它：

```bash
ssh -p 29902 ubuntu@hpc.vmip.com.cn 'bash /workspace/llamacpp/start_llm.sh'
```

> **踩坑提醒**：这个脚本自带 120 秒就绪检测，18 GB 的 MoE 模型从网络盘冷加载往往要
> 2–3 分钟，所以脚本大概率会打印 `[warn] 120s 内未检测到端口就绪`。
> **这不代表失败**，用上面的 `curl` 命令再查一次即可。

停止服务（一般不用）：

```bash
ssh -p 29902 ubuntu@hpc.vmip.com.cn 'bash /workspace/llamacpp/stop_llm.sh'
```

---

## 第 2 步 · 建立 SSH 隧道（双击即可）

双击：

```
E:\ai\storyforge\start_tunnel.bat
```

会弹出一个标题为 `StoryForge - GPU LLM Tunnel` 的命令行窗口，显示：

```
  Host   : ubuntu@hpc.vmip.com.cn:29902
  Forward: 127.0.0.1:8080  ->  127.0.0.1:8080

  Keep this window OPEN while using StoryForge.
```

**这个窗口不要关、不要按 Ctrl+C**。断线后脚本会自动每 5 秒重连一次。

> 想手动来也行：
> ```bash
> ssh -N -L 8080:127.0.0.1:8080 -p 29902 ubuntu@hpc.vmip.com.cn
> ```
> 执行后**没有任何输出、光标停住就是正常的**。

验证隧道通了（可选）：浏览器打开 `http://127.0.0.1:8080/v1/models`，能看到 `qwen3-30b-a3b` 就对了。

---

## 第 3 步 · 启动 StoryForge

双击：

```
E:\ai\storyforge\start.bat
```

首次运行会自动建虚拟环境、装依赖（1–3 分钟），之后秒开。
浏览器会自动打开工作台页面（地址通常是 `http://127.0.0.1:8000`，端口被占用会自动 +1，顶部会显示真实地址）。

---

## 第 4 步 · 配置区（① 配置区）

**LLM 接入：**

| 字段 | 填什么 |
|---|---|
| 模式 | 选 **「OpenAI 兼容接口」** |
| Base URL | `http://127.0.0.1:8080/v1` ← **结尾必须带 `/v1`** |
| 模型 | `qwen3-30b-a3b` |
| API Key | `sk-local-llamacpp`（非空即可） |

然后点 **「测试 LLM 连接」**，看到绿灯就 OK。

**视频生成：** 保持默认 **「模拟模式（Mock，零成本）」**，Key 留空。

最后点 **「保存配置」**。

> （这些值已经帮你写进 `config/config.json` 了，正常情况下打开就是填好的，直接点「测试 LLM 连接」即可。）

---

## 第 5 步 · 输入区（② 输入区）

1. **粗糙对话**：把你脑子里的一段剧情随手敲进去，不用管格式，例如：
   ```
   小明和小红在咖啡馆相遇，小红问小明要不要一起创业……
   ```
   懒得写就点 **「载入示例对话」** → 选一个 → 点「填充」。
2. **风格**：写实 / 动漫 / 动画
3. **镜头数**：2–8（建议首次用 4）
4. **每镜头时长**：2–10 秒（建议 4）
5. **分辨率**：720p（快）/ 1080p（慢）
6. **「跳过视频评估」**：勾上会省一半 LLM 调用时间，但报告里就没有视频评分了

点 **「开始生成」**。

---

## 第 6 步 · 看进度（③ 流水线进度）

四个阶段依次亮起：

1. `ScriptAgent` — 对话 → 结构化分镜脚本（最慢，30 秒内）
2. `DirectorAgent` — 逐镜头生成视频（Mock 模式每个镜头几秒）
3. `视频拼接` — FFmpeg 把分镜拼成成片
4. `CriticAgent` — 给脚本和视频打分

实时日志框可以看到每一步在干什么。单个镜头失败不会中断整个流程。

---

## 第 7 步 · 拿结果（④⑤ 结果预览 / 评估报告）

- **最终成片**：在预览区直接播放，点「下载成片」保存
- **分镜列表**：右侧逐个看镜头
- **评估报告**：脚本 4 个维度 + 视频 3 个维度的评分与中文点评，支持「下载评估 JSON」

所有产物按时间戳落在：

```
E:\ai\storyforge\outputs\<时间戳>\
├── script.json              分镜脚本
├── shots\shot_001.mp4 ...   单镜头视频
├── frames\                  末帧图（用于镜头间视觉连续）
├── final_video.mp4          最终成片
└── evaluation_report.json   评估报告
```

---

## 出错了怎么查

| 现象 | 原因 | 解决 |
|---|---|---|
| 顶部提示 `LLM_UNREACHABLE` / 无法连接 `127.0.0.1:8080` | SSH 隧道断了 | 重新双击 `start_tunnel.bat`，确认那个窗口开着 |
| 「测试 LLM 连接」返回 **404** | 模式选成了「本地 Ollama」 | 改成「**OpenAI 兼容接口**」 |
| 提示 `bind: Address already in use`（隧道窗口刷 warn） | 本机 8080 被占用 | `netstat -ano \| findstr 8080` 找到进程关掉；或改隧道端口为 `-L 18080:127.0.0.1:8080`，并把 Base URL 改成 `http://127.0.0.1:18080/v1` |
| 服务返回 `503 Loading model` 且持续很久 | 模型还在从网络盘加载 | 等 1–3 分钟；超过 5 分钟看 `ssh ... 'tail -50 /workspace/llamacpp/server.log'` |
| 提示 `FFM_NOT_FOUND` / 顶部红色 FFmpeg 提示 | FFmpeg 不在 PATH | 自行安装 FFmpeg 并加入 PATH（见《使用说明》「安装 FFmpeg」）；若需免安装分发，可用 `python scripts/build_exe.py` 重新打包便携版（内嵌 FFmpeg） |
| 生成很慢 / 超时 | 两路请求撞在一起抢显存 | 只跑一个任务；把镜头数/时长调小 |
| 想真出片但只有色块 | Mock 模式 | 需要接入真实视频 API（见下文） |

---

## 想真正出片？还差一步

Mock 出来的只是占位画面。要让镜头变成真实视频，二选一：

**方案 A — 用内置的 RunningHub 模型 API（推荐，改动最小，无需写代码）**

1. 去 <https://www.runninghub.cn> 拿到 **API Key**（⚠️ 模型 API 需**「企业级-共享」**Key）
2. 在配置区把「视频生成」切成「**RunningHub（模型 API）**」，填 Key，点「测试视频接口」
3. 直接开跑——适配器已实现完整的 **提交任务 → 轮询状态 → 下载 mp4 → 落盘 → 返回路径** 流程

```
app\services\adapters\runninghub_adapter.py   →  已实现（文生视频，无需改动）
```

> 默认 Base URL `https://www.runninghub.cn`、默认端点路径 `/openapi/v2/rhart-video-v3.1-lite-official/text-to-video`，均可在 `config.json` 的 `video_base_url` / `video_model` 覆盖。
> 如需扩展到别的厂商，只需在 `app/services/adapters/` 新增一个继承 `VideoGeneratorInterface` 的适配器并注册到 `ADAPTER_REGISTRY`，上层（DirectorAgent、UI、拼接）完全不用动。

**方案 B — 在服务器本地部署视频模型**

不推荐：这台服务器的 TITAN Xp 是 Pascal（sm_61）架构，FP16 算力被阉割到 1/64，
且主流视频模型的推理框架普遍要求新架构，实测跑不动。

---

## 附：服务器上的关键路径（排查用）

| 用途 | 路径 |
|---|---|
| 启动 / 停止脚本 | `/workspace/llamacpp/start_llm.sh`、`stop_llm.sh` |
| 服务日志 | `/workspace/llamacpp/server.log` |
| 模型 GGUF | `/workspace/llamacpp/qwen3-30b-a3b.gguf`（→ Ollama blob 软链，未额外占盘） |
| 关闭思考模式模板 | `/workspace/llamacpp/chat_template_qwen3_moe.jinja` |
| 二进制 | `/workspace/llamacpp/llama-b10985/llama-server` |
