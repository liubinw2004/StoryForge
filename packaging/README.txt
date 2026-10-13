StoryForge 使用说明（接收方部署版）
====================================

一、启动（两步走）
------------------
1. 双击 start_tunnel.bat —— 建立到共享 GPU 服务器的加密隧道，
   窗口会显示 "Forward: 127.0.0.1:8080"，请保持这个窗口一直开着。
2. 双击 StoryForge.exe —— 启动 StoryForge 本地服务，
   浏览器会自动打开 http://127.0.0.1:8000（未自动打开就手动输入）。

顺序不能反：必须先开隧道，再启动 StoryForge。
不用时：先关 StoryForge 窗口，再关隧道窗口。

二、AI 文本生成（LLM）
----------------------
已预配置通过隧道调用共享云端 GPU（qwen3-30b-a3b），开箱即用，
无需填写任何 Key。前提是 start_tunnel.bat 的窗口保持打开。

若想改用你自己的模型，用记事本打开 config/config.json 修改：
  - llm_mode      ：接口类型（openai 兼容 / ollama 等）
  - llm_base_url  ：你自己的模型接口地址
  - llm_model     ：模型名称
  - llm_api_key   ：接口 Key（本地 Ollama 可随意填占位值）
示例（本机 Ollama）：
  "llm_mode": "ollama",
  "llm_base_url": "http://127.0.0.1:11434/v1",
  "llm_model": "qwen2.5:14b"

三、视频生成（RunningHub 工作流 i2v）
------------------------------------
视频生成走 RunningHub，需要你自己填写 API Key：
1. 用记事本打开 config/config.json；
2. 把 video_api_key 的值（当前为空 ""）改成你自己的 RunningHub Key；
3. 如需更换工作流，可修改 rh_workflow_id；
4. 其余 rh_* 字段（节点 ID / 字段名 / 实例类型 / 保留时长）一般无需改动。
注意：RunningHub 视频生成按次计费，请确认 Key 有余额。

四、前置要求
------------
- 系统：Windows 10 / 11（自带 OpenSSH 客户端）。
  若双击 start_tunnel.bat 提示找不到 ssh，请启用：
  设置 -> 应用 -> 可选功能 -> 添加功能 -> OpenSSH 客户端。
- 无需安装 Python，主程序已内置运行环境。

五、文件结构
------------
  StoryForge.exe         主程序（免 Python）
  start_tunnel.bat       先运行它（建立 LLM 隧道）
  ssh_key/id_storyforge  隧道专用密钥（已内置，勿外传）
  config/config.json     配置（LLM 已预置；RunningHub Key 需自填）
  bin/ffmpeg.exe         FFmpeg 视频处理
  bin/ffprobe.exe        FFmpeg 媒体探测
  logs/                  运行日志
  outputs/               生成任务输出目录

六、常见问题
------------
- 页面打不开：确认 start_tunnel.bat 与 StoryForge 两个窗口都在运行，
  浏览器访问 http://127.0.0.1:8000
- LLM 报错连不上：先看隧道窗口是否还开着；隧道断了就重开 start_tunnel.bat
- 隧道提示 8080 被占用：关掉占用 8080 的程序后重开，或检查
  netstat -ano | findstr 8080
- 端口被占用：程序会自动用 8000 之后的空闲端口，实际端口见 logs/port.txt
- 提示 FFmpeg 缺失：确认 bin/ffmpeg.exe、bin/ffprobe.exe 与 exe 同级存在
