# 服务器 LLM 部署说明（llama.cpp）

> 面向**非专业用户**。照着本文一步步做，就能让 StoryForge 用上一台远程 GPU 服务器上的大模型。
> 全程只需 **2 条命令 + 改 4 个配置项**。
>
> 本项目在服务器上同时保留了两套推理引擎：
> **llama.cpp（首选，本文主线）** 与 **Ollama（备选，见 [附录 B](#附录-b备选方案ollama)）**。
> 两者用的是**同一份 GGUF 模型文件**，只是引擎不同。

---

## 目录

1. [这是什么](#1-这是什么)
2. [一键使用（照抄执行）](#2-一键使用照抄执行)
3. [验证是否连上](#3-验证是否连上)
4. [部署架构：模型跑在哪](#4-部署架构模型跑在哪)
5. [性能基准对比（llama.cpp vs Ollama，实测）](#5-性能基准对比llamacpp-vs-ollama实测)
6. [为什么选 qwen3:30b-a3b](#6-为什么选-qwen330b-a3b)
7. [核心改进：在服务端彻底关掉「思考模式」](#7-核心改进在服务端彻底关掉思考模式)
8. [显存占用与并发](#8-显存占用与并发)
9. [常见问题排查](#9-常见问题排查)
10. [附录 A：技术细节与服务器运维](#附录-a技术细节与服务器运维)
11. [附录 B：备选方案 —— Ollama](#附录-b备选方案ollama)

---

## 1. 这是什么

StoryForge 的"大脑"（生成分镜脚本、评估作品的大语言模型）**不跑在你的电脑上**，
而是跑在一台**远程 GPU 服务器**上。本版本改为用 **llama.cpp** 提供服务：

```
你的电脑 (StoryForge)                远程 GPU 服务器
┌──────────────────────┐            ┌──────────────────────────────────────┐
│  StoryForge Web 应用  │            │  2× NVIDIA TITAN Xp (12GB×2, Pascal) │
│  http://127.0.0.1:8000│            │  llama.cpp + qwen3:30b-a3b (Q4_K_M)  │
│         │            │            │         ▲                            │
│         │ HTTP       │   SSH 隧道  │         │ 127.0.0.1:8080             │
│         ▼            │◄──────────►│         │  (OpenAI 兼容 API)          │
│  127.0.0.1:8080 ─────┼────────────┼─────────┘                            │
└──────────────────────┘            └──────────────────────────────────────┘
       你在本机访问                   模型真正在 GPU 上跑
```

**关键点**

1. 服务器上的 llama.cpp 只监听它自己的 `127.0.0.1:8080`（**不对外开放**），
   所以必须用一条 **SSH 隧道** 把它"接到"你本机的 `127.0.0.1:8080`。
2. llama.cpp 提供的是**标准 OpenAI 兼容接口**（`/v1/chat/completions`、`/v1/models`），
   因此 StoryForge **必须用「OpenAI 兼容」模式**接入。
3. ⚠️ **llama.cpp 没有 Ollama 的 `/api/tags` 接口**，所以**不能用「本地 Ollama」模式**
   （这是与上一版 Ollama 部署最大的不同，详见 [第 9 节](#-测试-llm-报错-ollama-返回异常状态码--404)）。

---

## 2. 一键使用（照抄执行）

### 第 0 步：确认服务器上的服务在跑（通常已经在跑）

```bash
ssh hpc.vmip.com.cn 'bash /workspace/llamacpp/start_llm.sh'
```

看到 `[ok] 服务就绪: http://127.0.0.1:8080/v1/models` 即可。
若显示"已在运行"也**不用管**，说明服务本来就是好的。

### 第 1 步：建立 SSH 隧道

打开 **PowerShell** 或 **CMD**，粘贴执行：

```bash
ssh -N -L 8080:127.0.0.1:8080 -p 29902 ubuntu@hpc.vmip.com.cn
```

- 执行后**不会有任何输出**，光标停在原处 —— 这是**正常的**，说明隧道已建立。
- **这个窗口不要关闭、不要按 Ctrl+C**。关掉窗口 = 隧道断开。
- 想让它在后台运行（不占窗口），把 `-N` 换成 `-f -N`：

  ```bash
  ssh -f -N -L 8080:127.0.0.1:8080 -p 29902 ubuntu@hpc.vmip.com.cn
  ```

> 前提：你的电脑已经配置好免密登录（`~/.ssh/config` 里已配好 `hpc.vmip.com.cn`，密钥已授权）。
> 若 `ssh hpc.vmip.com.cn` 能免密登录，上面的命令可简写为
> `ssh -f -N -L 8080:127.0.0.1:8080 hpc.vmip.com.cn`。

### 第 2 步：配置 StoryForge

启动 StoryForge（双击 `start.bat`），在**「配置区」**按下表填写（**注意：与 Ollama 模式不同！**）：

| 配置项 | 填写内容 |
| --- | --- |
| **LLM 模式** | `OpenAI 兼容`（**不要**选「本地 Ollama」） |
| **Base URL** | `http://127.0.0.1:8080/v1`（**结尾必须带 `/v1`**） |
| **API Key** | `sk-local-llamacpp`（llama.cpp 不校验，但**必须非空**，随便填即可） |
| **模型名** | `qwen3-30b-a3b` |
| **视频来源** | `mock`（模拟） |

点击「**保存配置**」，再点「**测试 LLM**」。

> 💡 也可以直接改 `config/config.json`：
> ```json
> {
>   "llm_mode": "openai",
>   "llm_base_url": "http://127.0.0.1:8080/v1",
>   "llm_api_key": "sk-local-llamacpp",
>   "llm_model": "qwen3-30b-a3b",
>   "video_provider": "mock"
> }
> ```

### 第 3 步：开始使用

配置好后，回到主界面输入一段对话，点「开始生成」即可。
分镜脚本与评估报告这两步会由**远程 GPU 上的 qwen3:30b-a3b（llama.cpp）**完成，视频由本机 Mock 生成。

---

## 3. 验证是否连上

### 方法 A（推荐）：在 StoryForge 页面点「测试 LLM」

成功时会提示：

```
连接成功，检测到 1 个模型
```

模型列表应包含：`qwen3-30b-a3b`。（llama.cpp 是"单模型服务"，所以只有 1 个。）

### 方法 B：命令行直接验证

在任意终端执行：

```bash
curl http://127.0.0.1:8080/v1/models
```

返回类似：

```json
{"object":"list","data":[{"id":"qwen3-30b-a3b","object":"model", ...}]}
```

能看到 `qwen3-30b-a3b` 就说明**隧道通了、服务也在**。

### 方法 C：完整对话验证（含 `content` 非空检查）

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-30b-a3b","messages":[{"role":"user","content":"用一句话介绍你自己。"}],"max_tokens":128}'
```

**预期结果**（这是本方案最关键的验证点）：

| 字段 | 预期值 |
| --- | --- |
| `choices[0].message.content` | **非空**，直接是正文（如"我是通义千问……"） |
| `choices[0].message.reasoning_content` | **为空**（思考模式已在服务端关闭） |

实测返回（节选）：

```json
"message": {
  "role": "assistant",
  "content": "\n\n我是通义千问，阿里巴巴集团旗下的通义实验室自主研发的超大规模语言模型。……"
}
```

### 方法 D：一键端到端自检（项目内置脚本）

在 StoryForge 项目目录执行：

```bash
.venv\Scripts\python.exe scripts\e2e_llamacpp.py
```

它会：写临时配置 → 起应用 → 跑一次完整流水线（3 镜头 × 2 秒，Mock 视频）→
打印中文分镜脚本与评估报告 → 自动还原配置。全程约 **20 秒**。

---

## 4. 部署架构：模型跑在哪

| 项目 | 值 |
| --- | --- |
| 访问方式 | `ssh hpc.vmip.com.cn`（等价 `ssh -p 29902 ubuntu@hpc.vmip.com.cn`） |
| 操作系统 | Ubuntu 24.04.4 LTS (x86_64, 内核 6.8) |
| GPU | **2 × NVIDIA TITAN Xp，各 12 GB**（Pascal 架构，算力 sm_61） |
| 驱动 | 580.173.02 |
| CPU / 内存 | 16 核 / 32 GB |
| 磁盘 | `/workspace` 300 GB |
| **推理引擎** | **llama.cpp**（官方预编译包，版本 `b10985`，CUDA 12.8 构建） |
| 安装位置 | `/workspace/llamacpp/` |
| 模型文件 | `/workspace/llamacpp/qwen3-30b-a3b.gguf`（软链接 → Ollama blob，**同一份 GGUF，未额外下载**） |
| 聊天模板 | `/workspace/llamacpp/chat_template_qwen3_moe.jinja`（关闭思考模式用，见第 7 节） |
| 服务监听 | 服务器内 `127.0.0.1:8080`（**仅本机**，靠 SSH 隧道对外），API 路径 `/v1/*` |
| 上下文长度 | 16384 tokens |
| 启动脚本 | `/workspace/llamacpp/start_llm.sh` / `stop_llm.sh` |

### 启动 / 停止 / 查状态

```bash
# 启动（已在运行会提示，不会重复启动）
ssh hpc.vmip.com.cn 'bash /workspace/llamacpp/start_llm.sh'

# 停止
ssh hpc.vmip.com.cn 'bash /workspace/llamacpp/stop_llm.sh'

# 查状态（进程 + 健康检查）
ssh hpc.vmip.com.cn 'pgrep -af llamacpp/llama-b10985/llama-server; curl -s http://127.0.0.1:8080/v1/models'
```

> ⚠️ **llama.cpp 与 Ollama 抢同一块显存**：两者都吃满 2 张卡（约 22 GB），
> **不能同时运行**。切换时务必先停掉另一个：
> `bash /workspace/llamacpp/stop_llm.sh` ⇄ `bash /workspace/llm/stop_llm.sh`。

### 服务器目录结构

```
/workspace/
├── llamacpp/                              # ★ llama.cpp 部署（本方案）
│   ├── llama-b10985/                      #   预编译二进制 + libggml-cuda.so
│   ├── cudart-llama-b10985-bin-ubuntu-cuda-12.8-x64/   # CUDA 运行库
│   ├── qwen3-30b-a3b.gguf                 #   → Ollama blob 软链接（18 GB GGUF）
│   ├── chat_template_qwen3_moe.jinja      #   关闭思考模式的聊天模板
│   ├── start_llm.sh / stop_llm.sh         #   启停脚本
│   ├── bench_llamacpp.py                  #   性能基准脚本
│   └── server.log                         #   服务日志（排查用）
│
├── ollama/                                # 备选：Ollama 运行时（保留不动）
│   ├── bin/ollama  lib/ollama/  models/   #   models/ 里的 blob 就是上面的 GGUF
│   └── ollama.log
│
└── llm/                                   # 备选：Ollama 的启停与基准脚本
    ├── start_llm.sh / stop_llm.sh / bench_llm.py / accept_test.py
    └── Modelfile.nothink                  #   关闭思考用的 Modelfile（Ollama 路线）
```

---

## 5. 性能基准对比（llama.cpp vs Ollama，实测）

> 测试条件：2× TITAN Xp (Pascal)，`qwen3:30b-a3b` Q4_K_M（同一份 GGUF），100% 驻留显存。

### 5.1 引擎级吞吐（llama.cpp 官方基准 `llama-bench`）

| 引擎 | 提示处理 pp512 | 文本生成 tg128 |
| --- | --- | --- |
| **llama.cpp b10985** | **1277.8 tokens/s** | **83.9 tokens/s** |
| Ollama v0.34.0（文档基准） | ≈ 396 tokens/s | ≈ 82 tokens/s |

**结论：纯生成速度两者基本持平（83.9 vs 82）；llama.cpp 的提示处理快约 3 倍。**

### 5.2 StoryForge 端到端（同一套测试：3 镜头 × 2 秒，Mock 视频）

| 阶段 | llama.cpp | Ollama | 说明 |
| --- | --- | --- | --- |
| 脚本生成（ScriptAgent） | **9.5 s** | 42.0 s | 远程 LLM |
| 分镜生成（DirectorAgent） | 1.9 s | 1.8 s | Mock，不涉及 LLM |
| 视频拼接（FFmpeg） | 0.2 s | 0.2 s | 不涉及 LLM |
| 评估报告（CriticAgent） | **6.9 s** | 53.7 s | 远程 LLM |
| **合计** | **≈ 19.0 s** | **≈ 103.2 s** | llama.cpp 快 **≈ 5.43 倍** |

> 上表为 QA 独立复现值（llama.cpp 19.0 s vs Ollama 103.2 s，**≈ 5.43 倍**）；
> 工程侧首轮实测为 19.0 s vs 98.2 s（≈ 5.2 倍）。两者差异来自 Ollama 每次生成的思考长度波动。

### 5.3 为什么端到端能快 5.4 倍？

因为 **Ollama 没能真正关掉这个模型的"思考模式"**，而 llama.cpp 做到了：

| 同一句提问（"写一段三百字中文短文"） | 生成 token 数 | 其中思考内容 | 耗时 |
| --- | --- | --- | --- |
| Ollama（`think:false`） | 1300 | **非空**（`think:false` 被忽略） | 25.1 s |
| **llama.cpp（服务端关闭思考）** | ~420 | **空** | ~5 s |

Ollama 把 2/3 以上的算力花在了"自言自语"上，而这些思考内容
既拖慢速度、又挤占 `max_tokens`（额度耗尽时 `content` 会变空）。
llama.cpp 通过**服务端聊天模板**从根上关掉思考，于是**又快又稳**。
技术细节见 [第 7 节](#7-核心改进在服务端彻底关掉思考模式)。

### 5.4 模型加载与首 token 延迟（实测）

| 项目 | 实测值 | 说明 |
| --- | --- | --- |
| 服务**加载模型**（启动到 `/v1/models` 可访问） | **≈ 7 s** | 18 GB 权重从磁盘读入显存，重启服务后发生一次 |
| 真实大提示（StoryForge 脚本提示）**首次**调用 | **8.8 s** | 其中绝大部分是**大提示的预填充（prompt eval）**，与"冷启动"无关 |
| 同一提示**第二次**调用 | **8.2 s** | 已缓存前缀 |
| 小提示首 token 延迟 | 0.26–0.37 s | 服务已就绪、提示很短时的典型值 |

> ⚠️ 常见误读：**首次调用慢 ≠ 冷启动惩罚**。模型加载（≈7 s）只在服务启动时发生一次；
> 之后即使"第一次"提问，耗时也主要由**提示本身有多长**决定（预填充 ≈ 1277 tokens/s 按提示长度线性增加）。

---

## 6. 为什么选 qwen3:30b-a3b

| 原因 | 说明 |
| --- | --- |
| **MoE 架构，激活参数极少** | 总参数 30.5B，但每个 token 只激活 **3.3B**。TITAN Xp 是 Pascal 架构，**FP16 算力被硬件阉割到 1/64**，推理性能主要受**显存带宽**约束；激活参数少 → 每 token 要搬运的权重少 → **快得多**。 |
| **模型质量更强** | 30B 级 MoE 在中文创作与结构化 JSON 输出上的质量明显优于 14B 稠密模型。 |
| **显存刚好放得下** | Q4_K_M 量化后 17.3 GiB，两张 12 GB 卡分层分摊，100% 驻留 GPU，**无 CPU 回落**。 |
| **零额外下载** | 服务器上 Ollama 已经下过同一份 GGUF，llama.cpp 直接复用（软链接），省 18 GB 下载。 |

> 备选 `qwen2.5:14b`（9 GB，稠密）也在服务器上，若需**更省显存**可临时切换（见附录 B）。

---

## 7. 核心改进：在服务端彻底关掉「思考模式」

### 7.1 问题：`think: false` 为什么不管用？

服务器上的模型是 **`Qwen3 30B A3B Thinking 2507`**（`general.finetune = Thinking`），
是**纯思考型**模型。它自带一份聊天模板，生成提示永远是：

```
<|im_start|>assistant
<think>          ← 强制开启思考，模板里根本没有 enable_thinking 开关
```

所以：

- Ollama 的 `think: false` 参数**被这个模板忽略**（实测思考内容照旧产生）；
- llama.cpp 的 `--reasoning off` / `--chat-template-kwargs '{"enable_thinking": false}'`
  同样无效 —— 因为**模型自带的模板里没有 `enable_thinking` 这个变量**。

### 7.2 解法：给 llama.cpp 换一份支持开关的聊天模板

本项目提供 `/workspace/llamacpp/chat_template_qwen3_moe.jinja`，
在 `--reasoning off`（即 `enable_thinking=false`）时，生成提示改成**预填一个空的思考块**：

```
<|im_start|>assistant
<think>

</think>

```

模型看到"思考已经结束"，就会**直接输出正文** —— 这就是 Qwen3 官方标准的**非思考模式**写法。

启动服务时带上这两个参数即可：

```bash
--jinja --chat-template-file /workspace/llamacpp/chat_template_qwen3_moe.jinja --reasoning off
```

> 注：llama.cpp b10985 起，`--chat-template-kwargs '{"enable_thinking": false}'` 已标记为
> **deprecated**（会打印警告并提示改用 `--reasoning on/off`），故本项目统一使用 `--reasoning off`。
> `start_llm.sh` 里已经写好，**正常使用无需手动敲这些参数**。

### 7.3 效果

| | 思考内容 | `content` | 速度 |
| --- | --- | --- | --- |
| 默认（不处理） | 有（很长） | **可能为空**（额度被思考吃光） | 慢 |
| **本方案（服务端关闭）** | **无** | **一定有正文** | 快（≈5 倍） |

### 7.4 重要提醒：关闭思考不等于万能，请务必保留强 system 约束

本模型是 **Thinking 微调版**（`Qwen3 30B A3B Thinking 2507`）。即使已在服务端关闭思考，
它对**缺少强 system 约束**的提示**仍可能输出"类思考散文"而不是目标格式**。

- **QA 复现**：直接 `POST /v1/chat/completions`，只给一句"生成 8 个镜头的分镜 JSON"（无 system 约束）→
  模型返回大段**散文式分析**，`finish_reason=length`（额度耗尽，未产出 JSON）。
- **StoryForge 为什么没事**：因为 StoryForge 的提示词本来就带**强 system 约束** ——
  `app/services/prompt_templates.py` 的 `script_system_prompt()` 明确要求
  "只输出一个 JSON 对象，不要输出任何解释、标题、Markdown 代码块标记或多余文字"，
  并有 `app/services/llm_client.py` 的**空内容兜底**与 JSON 解析重试。
  实测真实提示**稳定输出合法 JSON**。

> **给调用方的建议**：无论用 llama.cpp 还是 Ollama 接入本模型，**都应保留强 system 约束**，
> 并在提示里**明确要求 JSON 输出**（最好同时给出字段 schema）。
> 不要指望"关闭思考"能替代提示工程 —— 它解决的是"思考占额度"，不是"指令遵循"。

---

## 8. 显存占用与并发

**实测显存占用**（`qwen3-30b-a3b`，上下文 16384）：

| GPU | `nvidia-smi` 实测占用 | 推理时利用率 |
| --- | --- | --- |
| GPU 0 | **≈ 11.15 GB / 12 GB** | 37–47% |
| GPU 1 | **≈ 10.68 GB / 12 GB** | 39–43% |

模型权重按**层（layer）**分摊到两张卡（`--split-mode layer -ngl 999`），**完全在 GPU 上推理**。

**并发说明**

- 两张卡合计约 24 GB 可用显存，`qwen3-30b-a3b` 单请求就吃掉约 22 GB → **同时只跑 1 路请求**。
- StoryForge 是"单任务串行"设计，**默认单请求场景下无影响**。
- 若需要多路并发，改用 `qwen2.5:14b`（9 GB，单卡可放）。

**上下文长度**：`start_llm.sh` 里设为 `16384`。分镜脚本这类任务通常只用 1–3 千 token，足够。
如需更长，可改脚本里的 `CTX=32768`，但要把 `UB` 降到 128（否则显存不足，见第 9 节）。

---

## 9. 常见问题排查

### ❓ 提示「无法连接 127.0.0.1:8080」/「LLM_UNREACHABLE」

**原因**：SSH 隧道断了。

**解决**

1. 检查是否有一个 `ssh -N -L 8080:...` 的窗口开着。**窗口关了隧道就没了**。
2. 重新执行 [第 2 节第 1 步](#第-1-步建立-ssh-隧道) 的命令。
3. 若提示 `bind: Address already in use`，说明本地 8080 端口被占用：

   ```bash
   netstat -ano | findstr 8080
   ```

   找到占用进程后，或改用其它本地端口（如 `-L 18080:127.0.0.1:8080`，
   同时把 StoryForge 的 Base URL 改成 `http://127.0.0.1:18080/v1`）。

---

### ❓ 「测试 LLM」报错 `Ollama 返回异常状态码` / 404

**原因**：你选了「**本地 Ollama**」模式，但 llama.cpp **没有 `/api/tags` 接口**。

**解决**：把「LLM 模式」改成「**OpenAI 兼容**」，Base URL 填 `http://127.0.0.1:8080/v1`。

> ✅ 记忆口诀：**llama.cpp = OpenAI 兼容模式；Ollama = 两种模式都行。**

---

### ❓ 报错「模型处于思考模式且未产出正文（`LLM_CONTENT_EMPTY`）」

**原因**：模型在思考，而 `max_tokens` 被思考内容吃光，`content` 为空。

**解决**

- 用本文的 llama.cpp 部署 + `start_llm.sh`（已关闭思考），**正常不会出现**。
- 如果你用的是 Ollama 路线：改用 [附录 B](#附录-b备选方案ollama) 的方式，或把模型换成
  `qwen2.5:14b`（非思考型）。
- 代码侧已兜底：未显式指定时 `max_tokens` 默认 4096，且会抛出**明确中文错误**而不是笼统的"空内容"。

---

### ❓ 直接调用模型时，返回的是"散文"而不是 JSON / `finish_reason=length`

**原因**：这是 **Thinking 微调版**模型（`Qwen3 30B A3B Thinking 2507`）的固有特性。
即使已在服务端关闭思考，**只要提示缺少强 system 约束**，它就可能改成"边分析边写"的散文，
把 `max_tokens` 耗尽（`finish_reason=length`），从而拿不到目标 JSON。

**解决**

- **保留强 system 约束**，并在提示里**明确要求只输出 JSON**（最好给出字段 schema）。
  StoryForge 已内置：`app/services/prompt_templates.py` 的 `script_system_prompt()` /
  `critic_*_system_prompt()` 都写了"只输出一个 JSON 对象，不要任何解释、代码块围栏"，
  配合 `app/services/llm_client.py` 的**空内容兜底**与 JSON 解析重试 ——
  **真实提示实测稳定输出合法 JSON**。
- 不要把"关闭思考"当成提示工程的替代品：它解决的是**思考占额度**，不是**指令遵循**。
- 更多背景见 [第 7.4 节](#74-重要提醒关闭思考不等于万能请务必保留强-system-约束)。

---

### ❓ 服务起不来 / 日志报 `libgomp.so.1: cannot open shared object file`

**原因**：服务器没有系统级 `libgomp`（无 apt 源）。

**解决**：`start_llm.sh` 已把 Ollama 自带的 `libgomp` 加进 `LD_LIBRARY_PATH`。
**请务必用脚本启动**，不要直接手敲 `./llama-server`。若仍报错，检查
`/workspace/ollama/lib/ollama/libgomp.so.1` 是否存在。

---

### ❓ 日志报 `cudaMalloc failed: out of memory` / `failed to allocate compute buffers`

**原因**：上下文太长或批太大，显存不够（常见于 32768 上下文 + `-ub 512`）。

**解决**：改 `/workspace/llamacpp/start_llm.sh` 里的参数组合：

| 组合 | 上下文 | 物理批 `UB` | 结果 |
| --- | --- | --- | --- |
| 默认 | 16384 | 512 | ✅ 可用（推荐，约占 11.1 / 10.7 GB） |
| 长上下文 | 32768 | 128 | ✅ 可用（约占 11.3 / 10.8 GB） |
| 显存不足 | 32768 | 512 | ❌ `out of memory` |

改完执行 `stop_llm.sh && start_llm.sh`。

---

### ❓ 速度突然变慢 / 疑似"回落 CPU"

**验证方法**

```bash
ssh hpc.vmip.com.cn 'nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv'
ssh hpc.vmip.com.cn 'grep -aE "eval time" /workspace/llamacpp/server.log | tail -3'
```

- 正常：两张卡各占约 10–11 GB、推理时利用率 35–50%；日志里 `eval time` 应显示 **≈ 78 tokens/s**。
- 若显存只有几百 MB，说明**模型跑在 CPU 上**：通常是 Ollama 与 llama.cpp 抢显存，
  执行 `bash /workspace/llm/stop_llm.sh && bash /workspace/llamacpp/stop_llm.sh && bash /workspace/llamacpp/start_llm.sh`。

---

### ❓ 想临时换回 Ollama

见 [附录 B](#附录-b备选方案ollama)。**注意先停掉 llama.cpp**（两者抢显存）。

---

## 附录 A：技术细节与服务器运维

<details>
<summary>点击展开（给维护者看）</summary>

### A.1 为什么官方预编译包能跑在 Pascal 上（本项目的关键结论）

TITAN Xp 是 **Pascal 架构（compute capability 6.1 / sm_61）**，比较老。
有个流传的说法是"**CUDA 12.8 起已弃用 Pascal，官方预编译包不含 sm_61 内核**"。

**本项目实测结论：这条说法对 llama.cpp b10985 的 CUDA 12.8 预编译包并不成立 —— 它可以直接用。**

决定性证据：

```
$ ./llama-server --list-devices
Available devices:
  CUDA0: NVIDIA TITAN Xp (12183 MiB, 848 MiB free)
  CUDA1: NVIDIA TITAN Xp (12175 MiB, 934 MiB free)

$ ./llama-bench -m qwen3-30b-a3b.gguf -ngl 999 -sm layer -p 512 -n 128 -r 2 -fa 0
| qwen3moe 30B.A3B Q4_K - Medium | 17.28 GiB | 30.53 B | CUDA | 999 | 0 | pp512 | 1277.83 ± 10.99 |
| qwen3moe 30B.A3B Q4_K - Medium | 17.28 GiB | 30.53 B | CUDA | 999 | 0 | tg128 |    83.94 ± 0.02 |
```

模型权重全量卸载到 GPU（`--split-mode layer`，两卡各约 9 GB 权重 + KV cache），
**因此不需要源码构建 CUDA 内核**，省去了安装 nvcc / 拼 CUDAToolkit root 的全部麻烦。

> 构建包信息：`llama-b10985-bin-ubuntu-cuda-12.8-x64.tar.gz` + `cudart-llama-b10985-bin-ubuntu-cuda-12.8-x64.tar.gz`
> （来自 GitHub `ggml-org/llama.cpp` release `b10985`）。

### A.2 服务器上的坑与变通

| 问题 | 变通 |
| --- | --- |
| 无 `nvcc` / 无 `/usr/local/cuda` | 用**官方预编译包**，无需编译 |
| 无系统 `libgomp.so.1`（无 apt 源、无系统 gcc） | 复用 `/workspace/ollama/lib/ollama/libgomp.so.1`，塞进 `LD_LIBRARY_PATH` |
| 无 `strings`/`readelf`（无 binutils） | 用 `grep -a` + Python 直接读文件校验 |
| 无 pip / 无 venv / PEP 668 限制 | 需要时用 `python3 get-pip.py --user --break-system-packages` 引导 |
| Ollama 与 llama.cpp 抢显存 | 两个启停脚本互斥，切换前先停 |

### A.3 `start_llm.sh` 完整参数说明

```bash
/workspace/llamacpp/llama-b10985/llama-server \
  -m /workspace/llamacpp/qwen3-30b-a3b.gguf \   # 模型（Ollama blob 的软链接）
  --host 127.0.0.1 --port 8080 \                # 只监听本机，靠隧道对外
  --alias qwen3-30b-a3b \                       # /v1/models 里显示的模型名
  -ngl 999 --split-mode layer \                 # 全部层卸载到 GPU；双卡按层分摊
  -c 16384 -ub 512 -b 2048 \                    # 上下文 16K；物理批 512；逻辑批 2048
  -fa off \                                     # Pascal 不支持 FlashAttention，必须关
  --jinja \                                     # 启用 Jinja 聊天模板
  --chat-template-file /workspace/llamacpp/chat_template_qwen3_moe.jinja \  # 关闭思考的模板
  --reasoning off                               # 关掉思考模式（enable_thinking=false）
```

### A.4 复现性能基准

```bash
# 1) 引擎级（llama-bench）—— 需先停掉正在跑的服务以腾出显存
ssh hpc.vmip.com.cn 'bash /workspace/llamacpp/stop_llm.sh'
ssh hpc.vmip.com.cn 'LD_LIBRARY_PATH=/workspace/llamacpp/llama-b10985:/workspace/llamacpp/cudart-llama-b10985-bin-ubuntu-cuda-12.8-x64:/workspace/ollama/lib/ollama \
  /workspace/llamacpp/llama-b10985/llama-bench -m /workspace/llamacpp/qwen3-30b-a3b.gguf -ngl 999 -sm layer -p 512 -n 128 -r 2 -fa 0'
ssh hpc.vmip.com.cn 'bash /workspace/llamacpp/start_llm.sh'

# 2) 应用级（OpenAI 兼容端点，含首 token 延迟）
ssh hpc.vmip.com.cn 'python3 /workspace/llamacpp/bench_llamacpp.py --max-tokens 512 --runs 2'

# 3) 场景级（StoryForge 提示词）
ssh hpc.vmip.com.cn 'python3 /workspace/llm/accept_test.py --base http://127.0.0.1:8080 --model qwen3-30b-a3b'
```

### A.5 客户端兼容性说明（StoryForge 侧）

- **llama.cpp → 必须用「OpenAI 兼容」模式**（无 `/api/tags`）：
  `llm_mode=openai`、`llm_base_url=http://127.0.0.1:8080/v1`、
  `llm_api_key` 任意非空（如 `sk-local-llamacpp`）、`llm_model=qwen3-30b-a3b`。
- 「OpenAI 兼容」模式**不会**注入 Ollama 私有的 `think` 参数 —— 这正是本方案的设计：
  **思考开关放在服务端**（模板 + `--reasoning off`），客户端保持"标准 OpenAI 客户端"，
  从而与 DeepSeek / 通义 / OpenAI 等**完全同构**，无需任何私有约定。
- 代码侧（`app/services/llm_client.py`）已具备：
  - 未显式指定 `max_tokens` 时默认 **4096**；
  - 当 `content` 为空但响应存在 `reasoning`/`reasoning_content`/`thinking` 时，
    抛出**明确中文错误** `LLM_CONTENT_EMPTY`（"模型处于思考模式且未产出正文……"），
    而不是笼统的"模型返回了空内容"。

</details>

---

## 附录 B：备选方案 —— Ollama

> Ollama 部署**完整保留**（`/workspace/ollama`，服务仍可正常启动），作为备选与对照。
> 它的优点是**「本地 Ollama」模式会自动注入 `think: false`**、以及提供 `/api/tags` 模型列表接口。

### B.1 启动 Ollama

```bash
# 先停掉 llama.cpp（两者抢显存）
ssh hpc.vmip.com.cn 'bash /workspace/llamacpp/stop_llm.sh'
# 启动 Ollama
ssh hpc.vmip.com.cn 'bash /workspace/llm/start_llm.sh'
```

### B.2 建立隧道（端口 11434）

```bash
ssh -f -N -L 11434:127.0.0.1:11434 -p 29902 ubuntu@hpc.vmip.com.cn
```

> ⚠️ **本机若也装了 Ollama**，它会占用 `0.0.0.0:11434`，导致隧道无法绑定。
> 用 `netstat -ano | findstr 11434` 确认；若是本机的 `ollama.exe`，先执行 `taskkill /IM ollama.exe /F`。
> 或改用别的本地端口：`-L 11834:127.0.0.1:11434`，并把 Base URL 填成 `http://localhost:11834`。

### B.3 StoryForge 配置（与 llama.cpp 不同！）

| 配置项 | 填写内容 |
| --- | --- |
| **LLM 模式** | `本地 Ollama` |
| **Base URL** | `http://localhost:11434` |
| **模型名** | `qwen3:30b-a3b` |
| **API Key** | 留空 |

### B.4 Ollama 的性能基准（对照）

| 模型 | 量化 | 生成速度 | 提示处理 |
| --- | --- | --- | --- |
| qwen3:30b-a3b | Q4_K_M | ≈ 82 tokens/s | 0.25 s / 99 tokens |
| qwen2.5:14b | Q4_K_M | ≈ 30 tokens/s | 0.26 s / 118 tokens |

> 端到端对比见 [第 5.2 节](#52-storyforge-端到端同一套测试3-镜头--2-秒mock-视频)：
> 同一任务 Ollama ≈ 103 s，llama.cpp ≈ 19 s（**≈ 5.43 倍**）。
> 原因：Ollama 的 `think: false` 对这个 Thinking 版模型不生效（仍产生思考内容）。

### B.5 Ollama 的启停

```bash
ssh hpc.vmip.com.cn 'bash /workspace/llm/start_llm.sh'   # 启动
ssh hpc.vmip.com.cn 'bash /workspace/llm/stop_llm.sh'    # 停止
```

---

*最后更新：2026-09-16 ｜ 部署方式：llama.cpp b10985（CUDA 12.8 预编译包）｜ 部署人：StoryForge 工程团队*
