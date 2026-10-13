/* ==========================================================================
   StoryForge 前端脚本（原生 JS，零依赖）
   职责：配置读写与连接测试 / 启动流水线 / 轮询进度 / 渲染预览与评估报告
   ========================================================================== */
(function () {
  "use strict";

  var POLL_INTERVAL = 1000; // 前端每 1000ms 轮询一次 /status
  var STAGE_KEYS = ["story", "script", "director", "concat", "critic"];
  var DOT_COUNT = 6;

  var EXAMPLES = [
    {
      name: "咖啡馆的约定（都市情感）",
      text:
        "小明和小红在咖啡馆相遇。小红推门进来，看到一个熟悉的身影——是大学同学小明。" +
        "两人愣了一下，随后笑着打招呼。聊着聊着，小红提起自己一直想开一家小店，" +
        "问小明要不要一起创业。小明想了很久，说他其实也有同样的想法，只是一直没敢说。" +
        "窗外阳光正好，两人举起咖啡杯，轻轻碰了一下。"
    },
    {
      name: "深夜便利店（悬疑）",
      text:
        "凌晨两点，一家 24 小时便利店。店员小林正在整理货架，一个穿黑风衣的男人走进来，" +
        "只看不买。小林觉得奇怪，注意到男人的袖口有暗红色的痕迹。男人突然走到收银台前，" +
        "问：「刚才有没有人来过？」小林摇头。男人留下一张纸条就离开了。" +
        "小林打开纸条，上面写着一串数字和一个地址。他犹豫了一下，把纸条塞进口袋。"
    },
    {
      name: "机器人学画画（科幻温情）",
      text:
        "在未来的城市里，一台家用机器人小 Q 被主人设定为「照料生活」。一天，主人把一盒旧颜料" +
        "忘在了桌上。小 Q 第一次见到颜料，试着在纸上涂抹。它画得很丑，但主人回来后却笑了，" +
        "说这是它见过最好的画。小 Q 的指示灯闪了闪，它决定每天都画一张。" +
        "多年后，主人老了，墙上贴满了小 Q 的画。"
    }
  ];

  var state = {
    taskId: null,
    pollTimer: null,
    story: null,
    script: null,
    report: null,
    finalReady: false,
    editClips: [],        // 当前剪辑方案（时间轴片段数据）
    editOriginal: [],     // 重建剪辑时用于「重置」的原始快照
    exported: null,       // 最近一次导出结果
    editDraftTimer: null, // 剪辑方案草稿保存的防抖定时器
    restoring: false,     // 是否处于「刷新后恢复」流程中
    zoom: 1,              // 时间轴缩放系数（P1 预留，默认 1）
    selectedClip: null,   // 当前选中的片段索引（供「分割」使用）
    playheadX: 0,         // 播放头在轨道内容中的像素横坐标（px）
    clipLayout: [],       // 每个片段在轨道上的像素布局：[{index,left,width}]
    reviewActive: false,  // 是否已进入分镜确认模式（避免每秒轮询覆盖用户编辑）
    chain: null,          // 串行首帧链状态（{segments, current_index, initial_frame, ...}）
    chainStarted: false   // 是否已上传初始图（决定是否显示上传区）
  };

  // 时间轴换算常量（见增量设计 §4.3）
  var PX_PER_SEC_BASE = 40;   // 基准比例尺：1 秒 = 40px
  var CLIP_MIN_WIDTH = 28;    // 片段最小宽度（保证可点选）
  var TRACK_GAP = 4;          // 片段间距（px）

  /* ------------------------------ 工具 ------------------------------ */
  function $(id) { return document.getElementById(id); }

  function toast(message, kind) {
    var el = $("toast");
    el.textContent = message;
    el.className = "toast " + (kind || "info");
    el.classList.remove("hidden");
    clearTimeout(el._timer);
    el._timer = setTimeout(function () { el.classList.add("hidden"); }, 3800);
  }

  async function apiGet(url) {
    var res = await fetch(url, { headers: { "Accept": "application/json" } });
    var json = null;
    try { json = await res.json(); } catch (e) { json = null; }
    if (!json) throw new Error("服务返回了非 JSON 响应（HTTP " + res.status + "）");
    return json;
  }

  async function apiPost(url, body) {
    var res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {})
    });
    var json = null;
    try { json = await res.json(); } catch (e) { json = null; }
    if (!json) throw new Error("服务返回了非 JSON 响应（HTTP " + res.status + "）");
    return json;
  }

  async function apiPut(url, body) {
    var res = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {})
    });
    var json = null;
    try { json = await res.json(); } catch (e) { json = null; }
    if (!json) throw new Error("服务返回了非 JSON 响应（HTTP " + res.status + "）");
    return json;
  }

  /* --------------------- 任务记忆（刷新后恢复） --------------------- */
  var LS_TASK = "storyforge_task_id";

  function rememberTask(taskId) {
    try {
      if (taskId) { window.localStorage.setItem(LS_TASK, taskId); }
      else { window.localStorage.removeItem(LS_TASK); }
    } catch (e) { /* 隐私模式 / 禁用存储：静默降级 */ }
  }

  function rememberedTask() {
    try { return window.localStorage.getItem(LS_TASK); }
    catch (e) { return null; }
  }

  function selectedRadio(name) {
    var el = document.querySelector('input[name="' + name + '"]:checked');
    return el ? el.value : "";
  }

  function setStatusLight(text, cls) {
    var el = $("configStatus");
    el.textContent = text;
    el.className = "status " + cls;
  }

  /* --------------------------- 表单 <-> 数据 --------------------------- */
  function fillRange(select, min, max, unit) {
    select.innerHTML = "";
    for (var i = min; i <= max; i++) {
      var opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = unit ? i + unit : String(i);
      select.appendChild(opt);
    }
  }

  function collectConfig() {
    var retain = parseInt($("rhRetainSeconds").value, 10);
    if (isNaN(retain) || retain < 10) { retain = 10; }
    if (retain > 180) { retain = 180; }

    return {
      llm_mode: selectedRadio("llm_mode"),
      llm_base_url: $("llmBaseUrl").value.trim(),
      llm_api_key: $("llmApiKey").value,
      llm_model: $("llmModel").value.trim(),
      video_provider: selectedRadio("video_provider"),
      video_api_key: $("videoApiKey").value,
      video_base_url: $("videoBaseUrl").value.trim(),
      video_model: "",
      consistency_lock: $("consistencyLock").checked,
      rh_workflow_id: $("rhWorkflowId").value.trim(),
      rh_instance_type: $("rhInstanceType").value,
      rh_retain_seconds: retain,
      rh_first_frame_node_id: $("rhFirstFrameNodeId").value.trim(),
      rh_first_frame_field_name: $("rhFirstFrameFieldName").value.trim(),
      rh_prompt_node_id: $("rhPromptNodeId").value.trim(),
      rh_prompt_field_name: $("rhPromptFieldName").value.trim(),
      rh_duration_node_id: $("rhDurationNodeId").value.trim(),
      rh_duration_field_name: $("rhDurationFieldName").value.trim(),
      rh_seed_node_id: $("rhSeedNodeId").value.trim(),
      rh_seed_field_name: $("rhSeedFieldName").value.trim(),
      resolution: $("resolutionSelect").value,
      style: $("styleSelect").value,
      shot_count: parseInt($("shotCountSelect").value, 10) || 4,
      shot_duration: parseInt($("shotDurationSelect").value, 10) || 4,
      skip_video_eval: $("skipVideoEval").checked
    };
  }

  function applyConfig(cfg) {
    if (!cfg) return;
    var mode = cfg.llm_mode || "ollama";
    document.querySelectorAll('input[name="llm_mode"]').forEach(function (el) {
      el.checked = (el.value === mode);
    });
    $("llmBaseUrl").value = cfg.llm_base_url || "";
    $("llmModel").value = cfg.llm_model || "";
    $("llmApiKey").value = cfg.llm_api_key || "";

    var provider = cfg.video_provider || "mock";
    document.querySelectorAll('input[name="video_provider"]').forEach(function (el) {
      el.checked = (el.value === provider);
    });
    $("videoApiKey").value = cfg.video_api_key || "";
    $("videoBaseUrl").value = cfg.video_base_url || "";
    // 一致性增量：缺省一律为 true（与后端默认值一致）
    $("consistencyLock").checked = cfg.consistency_lock !== false;

    // RunningHub 工作流 i2v 节点参数（缺省回退官方实测值）
    $("rhWorkflowId").value = cfg.rh_workflow_id || "2084067455902765057";
    $("rhInstanceType").value = cfg.rh_instance_type || "default";
    $("rhRetainSeconds").value = (cfg.rh_retain_seconds == null ? 180 : Number(cfg.rh_retain_seconds));
    $("rhFirstFrameNodeId").value = cfg.rh_first_frame_node_id || "4";
    $("rhFirstFrameFieldName").value = cfg.rh_first_frame_field_name || "image";
    $("rhPromptNodeId").value = cfg.rh_prompt_node_id || "7";
    $("rhPromptFieldName").value = cfg.rh_prompt_field_name || "prompt";
    $("rhDurationNodeId").value = cfg.rh_duration_node_id || "6";
    $("rhDurationFieldName").value = cfg.rh_duration_field_name || "duration_seconds";
    $("rhSeedNodeId").value = cfg.rh_seed_node_id || "9";
    $("rhSeedFieldName").value = cfg.rh_seed_field_name || "seed";

    $("resolutionSelect").value = cfg.resolution || "720p";
    $("styleSelect").value = cfg.style || "realistic";
    $("shotCountSelect").value = String(cfg.shot_count || 4);
    $("shotDurationSelect").value = String(cfg.shot_duration || 4);
    $("skipVideoEval").checked = !!cfg.skip_video_eval;

    syncModeUI();
  }

  /* --------------------------- 模式联动 --------------------------- */
  function syncModeUI() {
    var mode = selectedRadio("llm_mode");
    var isOllama = (mode === "ollama");
    $("llmKeyField").classList.toggle("hidden", isOllama);
    $("ollamaHint").classList.toggle("hidden", !isOllama);
    $("refreshModelsBtn").classList.toggle("hidden", !isOllama);
    if (isOllama && !$("llmBaseUrl").value.trim()) {
      $("llmBaseUrl").value = "http://localhost:11434";
    }
    if (!isOllama && /localhost:11434/.test($("llmBaseUrl").value)) {
      $("llmBaseUrl").value = "https://api.deepseek.com/v1";
      $("llmModel").value = $("llmModel").value === "qwen2.5:14b" ? "deepseek-chat" : $("llmModel").value;
    }

    var provider = selectedRadio("video_provider");
    var isMock = (provider === "mock");
    $("videoConfigFields").querySelectorAll("input").forEach(function (el) {
      el.disabled = isMock;
    });
    $("videoConfigFields").style.opacity = isMock ? "0.55" : "1";
  }

  /* --------------------------- 配置读取/保存 --------------------------- */
  async function loadConfig() {
    try {
      var res = await apiGet("/api/config");
      if (res.ok) { applyConfig(res.data); }
      else { toast(res.error || "读取配置失败", "err"); }
    } catch (e) {
      toast("读取配置失败：" + e.message, "err");
    }
  }

  async function saveConfig(silent) {
    try {
      var res = await apiPost("/api/config", collectConfig());
      if (res.ok) {
        applyConfig(res.data);
        if (!silent) toast("配置已保存", "ok");
        return true;
      }
      toast(res.error || "保存配置失败", "err");
      return false;
    } catch (e) {
      toast("保存配置失败：" + e.message, "err");
      return false;
    }
  }

  async function testLlm() {
    setStatusLight("测试中…", "light-run");
    try {
      var res = await apiPost("/api/config/test-llm", collectConfig());
      var data = res.data || {};
      if (data.ok) {
        setStatusLight("连接正常（" + (data.latency_ms || 0) + "ms，模型 " + (data.models || []).length + " 个）", "light-ok");
        toast(data.message || "LLM 连接正常", "ok");
        if (data.models && data.models.length) {
          fillModelOptions(data.models);
          if (!$("llmModel").value.trim()) { $("llmModel").value = data.models[0]; }
        }
      } else {
        setStatusLight("连接失败", "light-err");
        toast(data.message || res.error || "LLM 连接失败", "err");
      }
    } catch (e) {
      setStatusLight("连接失败", "light-err");
      toast("测试失败：" + e.message, "err");
    }
  }

  async function testVideo() {
    setStatusLight("测试中…", "light-run");
    try {
      var res = await apiPost("/api/config/test-video", collectConfig());
      var data = res.data || {};
      if (data.ok) {
        setStatusLight("视频接口就绪", "light-ok");
        toast(data.message || "视频接口就绪", "ok");
      } else {
        setStatusLight("视频接口未就绪", "light-err");
        toast(data.message || res.error || "视频接口不可用", "err");
      }
    } catch (e) {
      setStatusLight("视频接口未就绪", "light-err");
      toast("测试失败：" + e.message, "err");
    }
  }

  function fillModelOptions(models) {
    var list = $("modelOptions");
    list.innerHTML = "";
    (models || []).forEach(function (name) {
      var opt = document.createElement("option");
      opt.value = name;
      list.appendChild(opt);
    });
  }

  async function refreshOllamaModels() {
    try {
      var res = await apiGet("/api/config/ollama-models");
      var data = res.data || {};
      if (data.detected) {
        fillModelOptions(data.models);
        toast("已检测到 Ollama，共 " + (data.models || []).length + " 个模型", "ok");
      } else {
        toast(data.message || "未检测到 Ollama，可手动填写 Base URL", "err");
      }
    } catch (e) {
      toast("探测 Ollama 失败：" + e.message, "err");
    }
  }

  /* --------------------------- 流水线 --------------------------- */
  function validateInput() {
    var text = $("dialogue").value.trim();
    if (!text) {
      $("inputHint").textContent = "请先输入粗糙对话（或载入示例）";
      toast("请先输入粗糙对话", "err");
      $("dialogue").focus();
      return false;
    }
    $("inputHint").textContent = "";
    return true;
  }

  async function startPipeline() {
    if (!validateInput()) return;
    var payload = {
      dialogue: $("dialogue").value.trim(),
      style: $("styleSelect").value,
      shot_count: parseInt($("shotCountSelect").value, 10),
      shot_duration: parseInt($("shotDurationSelect").value, 10),
      resolution: $("resolutionSelect").value,
      skip_video_eval: $("skipVideoEval").checked,
      review: !!$("reviewToggle").checked      // 增量：生成前确认分镜（默认勾选）
    };
    var provider = selectedRadio("video_provider");
    if (provider !== "mock") {
      if (!window.confirm("当前使用真实视频接口（" + provider + "），可能产生费用。确认继续？")) { return; }
    }

    await saveConfig(true);

    $("runBtn").disabled = true;
    resetProgressUI();
    appendLogLine("正在创建任务…");
    try {
      var res = await apiPost("/api/pipeline/run", payload);
      if (!res.ok) {
        toast(res.error || "启动失败", "err");
        $("runBtn").disabled = false;
        return;
      }
      state.taskId = res.data.task_id;
      state.finalReady = false;
      rememberTask(state.taskId);
      $("viewStoryBtn").disabled = true;
      $("viewScriptBtn").disabled = true;
      $("downloadFinalBtn").disabled = true;
      $("downloadReportBtn").disabled = true;
      toast("任务已启动：" + state.taskId, "ok");
      startPolling();
    } catch (e) {
      toast("启动失败：" + e.message, "err");
      $("runBtn").disabled = false;
    }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(pollStatus, POLL_INTERVAL);
    pollStatus();
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function pollStatus() {
    if (!state.taskId) return;
    try {
      var res = await apiGet("/api/pipeline/" + state.taskId + "/status");
      if (!res.ok) {
        stopPolling();
        $("runBtn").disabled = false;
        toast(res.error || "查询状态失败", "err");
        return;
      }
      renderStatus(res.data);
      if (res.data.state === "awaiting_review") {
        // 非终态：脚本已就绪、等待人工确认。保持轮询，但确认区只「首次」渲染，
        // 避免每秒重绘覆盖用户正在编辑的内容（AC2）。
        if (!state.reviewActive) { await enterReviewMode(res.data); }
        return;
      }
      // 导演阶段 running：串行首帧链等待上传初始图 / 逐段生成中 → 轮询链状态
      var director = res.data.stages && res.data.stages.director;
      if (res.data.state === "running" && director && director.state === "running") {
        await pollChain();
      }
      if (res.data.state === "success" || res.data.state === "failed") {
        stopPolling();
        $("runBtn").disabled = false;
        await finalizeTask(res.data);
      }
    } catch (e) {
      // 轮询偶发失败不中断，等待下次
    }
  }

  async function finalizeTask(status, restoreMode) {
    exitReviewMode(); // 任务进入终态：收起确认区（若曾显示）
    try {
      var stres = await apiGet("/api/pipeline/" + state.taskId + "/story");
      if (stres.ok) {
        state.story = stres.data;
        $("viewStoryBtn").disabled = false;
        $("storyRerunBtn").disabled = false;
        fillStoryForm(stres.data);
      }
    } catch (e) { /* ignore */ }

    // 恢复历史任务时，把原始粗糙对话回填到输入区（若用户尚未输入内容）
    if (state.story && state.story.source_dialogue && !$("dialogue").value.trim()) {
      $("dialogue").value = state.story.source_dialogue;
    }

    if (status.script) {
      state.script = status.script;
      $("viewScriptBtn").disabled = false;
    }
    try {
      var sres = await apiGet("/api/pipeline/" + state.taskId + "/script");
      if (sres.ok) {
        state.script = sres.data;
        $("viewScriptBtn").disabled = false;
        $("scriptJson").textContent = JSON.stringify(sres.data, null, 2);
      }
    } catch (e) { /* ignore */ }

    var hasSuccess = (status.shots || []).some(function (s) { return s.state === "success"; });
    if (hasSuccess && status.stages && status.stages.concat && status.stages.concat.state === "success") {
      var url = "/api/pipeline/" + state.taskId + "/video/final_video.mp4";
      $("finalVideo").src = url;
      state.finalReady = true;
      $("downloadFinalBtn").disabled = false;
      $("finalMeta").textContent = "可播放/下载（" + (status.output_dir || "") + "/final_video.mp4）";

      // 构建剪辑区（默认：原始顺序、全启用、无裁剪）
      state.editClips = buildEditClips(status.shots, defaultShotDuration());
      // 「重置剪辑」的基准 = 原始顺序（必须在套用草稿之前快照）
      state.editOriginal = JSON.parse(JSON.stringify(state.editClips));
      state.selectedClip = null;
      state.playheadX = 0;
      resetExportResult();

      // 恢复上次保存的剪辑方案（无草稿则保持原始）
      var restored = applyDraft(await loadDraft());
      renderTimeline();
      updateEditSummary();
      if (restored > 0) {
        $("editExportHint").textContent =
          "已恢复上次的剪辑方案（" + restored + " / " + state.editClips.length + " 个片段）";
      }
    }

    try {
      var rres = await apiGet("/api/pipeline/" + state.taskId + "/report");
      if (rres.ok) {
        state.report = rres.data;
        renderReport(rres.data);
        $("downloadReportBtn").disabled = false;
      }
    } catch (e) { /* ignore */ }

    if (restoreMode) return; // 恢复流程由调用方给出提示，避免出现「任务完成」的误导文案

    if (status.state === "failed") {
      toast("任务失败：" + (status.error || "未知错误"), "err");
    } else if (status.state === "success") {
      toast("任务完成 🎉", "ok");
    }
  }

  /* ------------------- 刷新后恢复上次的任务会话 ------------------- */
  async function restoreSession() {
    var taskId = rememberedTask();
    if (!taskId) return;

    var res = null;
    try { res = await apiGet("/api/pipeline/" + taskId + "/status"); }
    catch (e) { return; } // 后端暂时不可达：保持干净初始态，不做任何破坏
    if (!res.ok) {
      // 任务已不存在（服务重启后内存任务表清空 / 产物被清理）
      rememberTask(null);
      return;
    }

    var status = res.data;
    state.taskId = taskId;
    state.restoring = true;

    if (status.state === "awaiting_review") {
      $("runBtn").disabled = true;
      renderStatus(status);
      await enterReviewMode(status);
      startPolling();
      toast("已恢复上次任务 " + taskId + "，等待确认分镜", "ok");
      state.restoring = false;
      return;
    }

    if (status.state === "running" || status.state === "pending") {
      $("runBtn").disabled = true;
      appendLogLine("已恢复任务 " + taskId + "，继续监听进度…");
      startPolling();
      toast("已恢复上次任务 " + taskId + "，继续监听进度", "ok");
      state.restoring = false;
      return;
    }

    renderStatus(status);
    await finalizeTask(status, true);
    toast("已恢复上次任务 " + taskId + "（含剪辑方案）", "ok");
    state.restoring = false;
  }

  /* --------------------------- 渲染：进度 --------------------------- */
  function resetProgressUI() {
    STAGE_KEYS.forEach(function (key) {
      var stage = document.querySelector('.stage[data-stage="' + key + '"]');
      stage.setAttribute("data-state", "pending");
      stage.querySelector("[data-detail]").textContent = "等待中";
      renderDots(stage.querySelector("[data-dots]"), 0, "pending");
    });
    $("shotsRow").innerHTML = "";
    $("logBox").textContent = "（暂无日志）";
    $("shotList").innerHTML = '<div class="empty">尚无分镜</div>';
    $("shotSummary").textContent = "";
    $("finalVideo").removeAttribute("src");
    $("reportBody").innerHTML = '<div class="empty">尚无评估报告</div>';

    // 剪辑区复位
    stopEditPlayback();
    clearTimeout(state.editDraftTimer);
    state.editDraftTimer = null;
    state.story = null;
    state.editClips = [];
    state.editOriginal = [];
    state.exported = null;
    state.selectedClip = null;
    state.playheadX = 0;
    state.clipLayout = [];
    exitReviewMode();
    clearStoryForm();
    clearReviewForm();
    resetChainUI();
    $("viewStoryBtn").disabled = true;
    $("storyRerunBtn").disabled = true;
    $("editList").innerHTML = '<div class="empty">尚无片段</div>';
    $("timelineRuler").innerHTML = "";
    $("timelinePlayhead").classList.add("hidden");
    $("splitClipBtn").disabled = true;
    $("editSummary").textContent = "预计时长 —";
    $("editExportHint").textContent = "";
    $("previewPlayer").removeAttribute("src");
    resetExportResult();
    $("exportBtn").disabled = true;
    $("resetEditBtn").disabled = true;
    $("playEditBtn").disabled = true;
  }

  function renderDots(container, filled, stateName) {
    container.innerHTML = "";
    for (var i = 0; i < DOT_COUNT; i++) {
      var dot = document.createElement("span");
      dot.className = "dot" + (i < filled ? " done" : "");
      container.appendChild(dot);
    }
  }

  function renderStatus(status) {
    var stages = status.stages || {};
    STAGE_KEYS.forEach(function (key) {
      var stage = document.querySelector('.stage[data-stage="' + key + '"]');
      var st = stages[key] || { state: "pending", detail: "" };
      stage.setAttribute("data-state", st.state);
      var filled = st.state === "success" || st.state === "failed" ? DOT_COUNT
        : st.state === "running" ? Math.floor(DOT_COUNT / 2) : 0;
      renderDots(stage.querySelector("[data-dots]"), filled, st.state);
      var detail = st.detail || "";
      if (st.elapsed) { detail += (detail ? " · " : "") + st.elapsed + "s"; }
      if (!detail) { detail = st.state === "pending" ? "等待中" : st.state; }
      stage.querySelector("[data-detail]").textContent = detail;
    });

    renderShots(status.shots || []);
    renderLogs(status.logs || []);
  }

  function renderShots(shots) {
    var row = $("shotsRow");
    row.innerHTML = "";
    shots.forEach(function (shot) {
      var chip = document.createElement("span");
      chip.className = "shot-chip " + (shot.state || "waiting");
      var dot = document.createElement("span");
      dot.className = "dot-sm";
      chip.appendChild(dot);
      chip.appendChild(document.createTextNode(shot.shot_id + " " + stateLabel(shot.state)));
      if (shot.error) { chip.title = shot.error; }
      row.appendChild(chip);
    });

    var list = $("shotList");
    list.innerHTML = "";
    var successCount = 0;
    if (!shots.length) {
      list.innerHTML = '<div class="empty">尚无分镜</div>';
    }
    shots.forEach(function (shot) {
      if (shot.state === "success") successCount++;
      var item = document.createElement("div");
      item.className = "shot-item";
      var nameBox = document.createElement("span");
      nameBox.className = "name";
      nameBox.innerHTML = '<span class="shot-badge ' + (shot.state || "waiting") + '">' +
        stateLabel(shot.state) + '</span>' + shot.shot_id;
      item.appendChild(nameBox);

      var btns = document.createElement("span");
      btns.className = "actions-row";
      btns.style.marginTop = "0";

      if (shot.state === "success" && shot.video_path) {
        var play = document.createElement("button");
        play.className = "btn btn-mini";
        play.textContent = "播放";
        play.onclick = function () { playShot(shot.video_path); };
        btns.appendChild(play);

        if (shot.frame_path) {
          var frameBtn = document.createElement("button");
          frameBtn.className = "btn btn-mini btn-ghost";
          frameBtn.textContent = "末帧";
          frameBtn.title = "查看该镜头末帧（下一镜头的参考帧）";
          frameBtn.onclick = function () {
            window.open("/api/pipeline/" + state.taskId + "/frames/" +
              shot.frame_path.split("/").pop(), "_blank");
          };
          btns.appendChild(frameBtn);
        }

        var dl = document.createElement("button");
        dl.className = "btn btn-mini btn-ghost";
        dl.textContent = "下载";
        dl.onclick = function () { downloadArtifact(shot.video_path); };
        btns.appendChild(dl);
      }
      item.appendChild(btns);
      list.appendChild(item);
    });

    if (shots.length) {
      var total = shots.reduce(function (sum, s) { return sum + (s.state === "success" ? 1 : 0); }, 0);
      $("shotSummary").textContent = "共 " + shots.length + " 镜头 · 成功 " + successCount + " 个";
    }
  }

  function stateLabel(st) {
    return {
      waiting: "等待", running: "生成中", success: "完成", failed: "失败",
      awaiting_review: "待确认"
    }[st] || st;
  }

  function renderLogs(logs) {
    var box = $("logBox");
    if (!logs.length) { box.textContent = "（暂无日志）"; return; }
    var atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.textContent = logs.join("\n");
    if (atBottom) { box.scrollTop = box.scrollHeight; }
  }

  function appendLogLine(line) {
    var box = $("logBox");
    if (box.textContent === "（暂无日志）") { box.textContent = ""; }
    box.textContent += (box.textContent ? "\n" : "") + line;
    box.scrollTop = box.scrollHeight;
  }

  function playShot(path) {
    var url = "/api/pipeline/" + state.taskId + "/video/" + path;
    $("finalVideo").src = url;
    $("finalVideo").play().catch(function () { /* 自动播放可能被拦截 */ });
    document.getElementById("section-preview").scrollIntoView({ behavior: "smooth" });
  }

  function downloadArtifact(name) {
    window.location.href = "/api/pipeline/" + state.taskId + "/download-artifact?name=" + encodeURIComponent(name);
  }

  /* --------------------------- 串行首帧链（逐段确认） --------------------------- */
  function chainLabel(st) {
    return {
      pending: "等待", uploading: "上传首帧", submitting: "提交中",
      polling: "轮询中", awaiting_confirm: "待确认", accepted: "已接受",
      failed: "失败"
    }[st] || st;
  }

  function chainFrameUrl(relPath) {
    if (!relPath) return "";
    return "/api/pipeline/" + state.taskId + "/frames/" + relPath.split("/").pop();
  }

  function chainVideoUrl(relPath) {
    if (!relPath) return "";
    return "/api/pipeline/" + state.taskId + "/video/" + relPath;
  }

  function resetChainUI() {
    state.chain = null;
    state.chainStarted = false;
    $("section-chain").classList.add("hidden");
    $("chainChip").textContent = "—";
    $("chainChip").className = "chip";
    $("chainHint").textContent =
      "脚本已就绪：请先上传一张「初始图」，程序会把它作为第 1 段首帧；第 2 段起自动承接上一段尾帧。" +
      "每段生成完成后请点「接受并继续」，全部接受后自动拼接成片。";
    $("chainStartArea").classList.remove("hidden");
    $("chainFileInput").value = "";
    $("chainSegments").innerHTML = "";
  }

  async function pollChain() {
    if (!state.taskId) return;
    try {
      var res = await apiGet("/api/pipeline/" + state.taskId + "/chain");
      if (!res.ok) return;
      var chain = res.data.chain || { segments: [], current_index: 0 };
      state.chain = chain;
      state.chainStarted = (chain.segments || []).length > 0;
      $("section-chain").classList.remove("hidden");
      renderChain(chain);
    } catch (e) { /* 轮询偶发失败不中断，等待下次 */ }
  }

  function renderChain(chain) {
    chain = chain || { segments: [], current_index: 0 };
    var segs = chain.segments || [];
    var chip = $("chainChip");

    var allAccepted = segs.length > 0 && segs.every(function (s) { return s.state === "accepted"; });
    var anyFailed = segs.some(function (s) { return s.state === "failed"; });
    if (allAccepted) {
      chip.textContent = "✓ 全部接受，正在拼接成片";
      chip.className = "chip ok";
    } else if (anyFailed) {
      chip.textContent = "有段生成失败，请重生成本段";
      chip.className = "chip warn";
    } else if (segs.length) {
      chip.textContent = "当前第 " + (chain.current_index || 1) + " / " + segs.length + " 段";
      chip.className = "chip";
    } else {
      chip.textContent = "等待上传初始图";
      chip.className = "chip";
    }

    $("chainStartArea").classList.toggle("hidden", state.chainStarted);

    var box = $("chainSegments");
    box.innerHTML = "";
    if (!segs.length) {
      box.innerHTML = '<div class="empty">尚无段落——上传初始图后开始逐段生成</div>';
      return;
    }
    segs.forEach(function (seg) { box.appendChild(buildChainSegment(seg, chain)); });
  }

  function buildChainSegment(seg, chain) {
    var card = document.createElement("div");
    card.className = "chain-segment";
    card.dataset.index = String(seg.index);

    var head = document.createElement("div");
    head.className = "chain-seg-head";
    var title = document.createElement("span");
    title.className = "chain-seg-title";
    title.textContent = "第 " + seg.index + " 段 · segment_" + ("00" + seg.index).slice(-3);
    head.appendChild(title);
    var badge = document.createElement("span");
    badge.className = "chain-badge " + (seg.state || "pending");
    badge.textContent = chainLabel(seg.state);
    head.appendChild(badge);
    card.appendChild(head);

    var media = document.createElement("div");
    media.className = "chain-media";

    // 首帧
    var fwrap = document.createElement("div");
    fwrap.className = "chain-frame";
    var fl = document.createElement("span");
    fl.className = "field-label";
    fl.textContent = "首帧";
    fwrap.appendChild(fl);
    var fimg = document.createElement("img");
    fimg.className = "chain-thumb";
    fimg.alt = "首帧";
    var firstSrc = seg.first_frame_source || chain.initial_frame || "";
    if (firstSrc) { fimg.src = chainFrameUrl(firstSrc); }
    fwrap.appendChild(fimg);
    media.appendChild(fwrap);

    // 本段视频
    var vwrap = document.createElement("div");
    vwrap.className = "chain-video-wrap";
    var vl = document.createElement("span");
    vl.className = "field-label";
    vl.textContent = "本段视频";
    vwrap.appendChild(vl);
    var v = document.createElement("video");
    v.className = "chain-video";
    v.controls = true;
    v.preload = "metadata";
    if (seg.video_path) { v.src = chainVideoUrl(seg.video_path); }
    vwrap.appendChild(v);
    media.appendChild(vwrap);

    // 尾帧
    var twrap = document.createElement("div");
    twrap.className = "chain-frame";
    var tl = document.createElement("span");
    tl.className = "field-label";
    tl.textContent = "尾帧（下一段首帧）";
    twrap.appendChild(tl);
    var timg = document.createElement("img");
    timg.className = "chain-thumb";
    timg.alt = "尾帧";
    if (seg.tail_frame_path) { timg.src = chainFrameUrl(seg.tail_frame_path); }
    twrap.appendChild(timg);
    media.appendChild(twrap);

    card.appendChild(media);

    if (seg.error) {
      var err = document.createElement("div");
      err.className = "chain-error";
      err.textContent = "错误：" + seg.error;
      card.appendChild(err);
    }

    var actions = document.createElement("div");
    actions.className = "chain-actions";
    if (seg.state === "awaiting_confirm") {
      var accept = document.createElement("button");
      accept.className = "btn btn-primary btn-mini";
      accept.textContent = (seg.index === segsTotal(chain)) ? "✓ 接受并完成" : "✓ 接受并继续";
      accept.onclick = function () { chainAccept(seg.index); };
      actions.appendChild(accept);

      var regen = document.createElement("button");
      regen.className = "btn btn-ghost btn-mini";
      regen.textContent = "重生成本段";
      regen.onclick = function () { chainRegenerate(seg.index); };
      actions.appendChild(regen);
    } else if (seg.state === "failed") {
      var regen2 = document.createElement("button");
      regen2.className = "btn btn-mini";
      regen2.textContent = "重生成本段（换种子）";
      regen2.onclick = function () { chainRegenerate(seg.index); };
      actions.appendChild(regen2);
    }
    card.appendChild(actions);
    return card;
  }

  function segsTotal(chain) {
    return (chain && chain.segments && chain.segments.length) || 0;
  }

  async function chainStart() {
    var file = $("chainFileInput").files[0];
    if (!file) { toast("请先选择一张初始图", "err"); return; }
    var fd = new FormData();
    fd.append("file", file);
    $("chainStartBtn").disabled = true;
    $("chainStartBtn").textContent = "上传中…";
    try {
      var res = await fetch("/api/pipeline/" + state.taskId + "/chain/start", { method: "POST", body: fd });
      var json = await res.json();
      if (json.ok) {
        toast("初始图已上传，开始逐段生成", "ok");
        state.chainStarted = true;
        state.chain = json.data.chain;
        renderChain(state.chain);
        pollStatus();
      } else {
        toast(json.error || "启动首帧链失败", "err");
      }
    } catch (e) {
      toast("启动首帧链失败：" + e.message, "err");
    } finally {
      $("chainStartBtn").disabled = false;
      $("chainStartBtn").textContent = "上传并开始";
    }
  }

  async function chainAccept(index) {
    try {
      var res = await apiPost("/api/pipeline/" + state.taskId + "/chain/segments/" + index + "/accept", {});
      if (res.ok) {
        toast("第 " + index + " 段已接受", "ok");
        state.chain = res.data.chain;
        renderChain(state.chain);
        pollStatus();
      } else {
        toast(res.error || "接受失败", "err");
      }
    } catch (e) {
      toast("接受失败：" + e.message, "err");
    }
  }

  async function chainRegenerate(index) {
    try {
      var res = await apiPost("/api/pipeline/" + state.taskId + "/chain/segments/" + index + "/regenerate", {});
      if (res.ok) {
        toast("第 " + index + " 段已重新提交（换种子）", "ok");
        state.chain = res.data.chain;
        renderChain(state.chain);
        pollStatus();
      } else {
        toast(res.error || "重生成失败", "err");
      }
    } catch (e) {
      toast("重生成失败：" + e.message, "err");
    }
  }

  /* --------------------------- 视频剪辑区（时间轴编辑器） --------------------------- */
  function defaultShotDuration() {
    return parseInt($("shotDurationSelect").value, 10) || 4;
  }

  function cloneClip(clip) { return JSON.parse(JSON.stringify(clip)); }

  function effectiveId(clip) { return clip.clip_id || clip.shot_id; }

  function pxPerSec() { return PX_PER_SEC_BASE * (state.zoom || 1); }

  function clipDuration(clip) {
    var start = Number(clip.start) || 0;
    var end = (clip.end == null ? clip.orig_duration : Number(clip.end));
    return Math.max(0, end - start);
  }

  // 片段像素宽度 ∝ 裁剪后时长（保底可点选，见设计 §4.3）
  function clipWidth(clip) { return Math.max(CLIP_MIN_WIDTH, clipDuration(clip) * pxPerSec()); }

  function fmtTime(sec) { return (Math.round(sec * 10) / 10) + "s"; }

  function buildEditClips(shots, fallbackDuration) {
    var clips = [];
    (shots || []).forEach(function (shot) {
      if (shot.state !== "success" || !shot.video_path) return;
      var dur = Number(shot.duration || 0);
      var trimmable = dur > 0;
      clips.push({
        shot_id: shot.shot_id,
        clip_id: null,                    // 未分割时为空；有效 id = clip_id || shot_id
        video_path: shot.video_path,
        frame_path: shot.frame_path || "",
        orig_duration: trimmable ? dur : (fallbackDuration || 0),
        trimmable: trimmable,
        start: 0,
        end: trimmable ? dur : null,
        enabled: true
      });
    });
    return clips;
  }

  function updateEditSummary() {
    var total = 0;
    var count = 0;
    state.editClips.forEach(function (clip) {
      if (clip.enabled) { total += clipDuration(clip); count++; }
    });
    $("editSummary").textContent =
      "启用 " + count + " / " + state.editClips.length + " 个片段 · 预计时长 " + total.toFixed(1) + "s";
    $("exportBtn").disabled = !state.taskId || count === 0;
    $("resetEditBtn").disabled = !state.editClips.length;
    $("playEditBtn").disabled = count === 0;
  }

  /* ------------------- 剪辑方案草稿（刷新后自动恢复） ------------------- */
  // 设计取舍：草稿落盘在**服务端**（outputs/<ts>/edit_plan_draft.json），而非浏览器，
  // 这样换浏览器 / 清缓存 / 换设备都能恢复；仅任务 ID 存 localStorage 作入口。
  function currentEdl() {
    return state.editClips.map(function (clip, index) {
      return {
        shot_id: clip.shot_id,
        clip_id: (clip.clip_id == null ? null : clip.clip_id),
        enabled: !!clip.enabled,
        start: Number(clip.start) || 0,
        end: (clip.end == null ? null : Number(clip.end)),
        order: index
      };
    });
  }

  function scheduleDraftSave() {
    if (!state.taskId) return;
    clearTimeout(state.editDraftTimer);
    // 防抖 400ms：拖拽/连点开关时避免每帧都发请求
    state.editDraftTimer = setTimeout(function () { saveDraft(true); }, 400);
  }

  async function saveDraft(silent) {
    if (!state.taskId) return;
    try {
      var res = await apiPut("/api/pipeline/" + state.taskId + "/edit-plan", { clips: currentEdl() });
      if (!res.ok && !silent) { toast(res.error || "保存剪辑方案失败", "err"); }
    } catch (e) {
      // 草稿保存属于体验优化：失败静默降级，不影响预览与导出
    }
  }

  async function loadDraft() {
    if (!state.taskId) return [];
    try {
      var res = await apiGet("/api/pipeline/" + state.taskId + "/edit-plan");
      if (res.ok && res.data && Array.isArray(res.data.clips)) { return res.data.clips; }
    } catch (e) { /* ignore */ }
    return [];
  }

  // 按**有效 id** 重建剪辑：支持 split（同 shot 多段）；旧草稿（无 clip_id）逐字还原为
  // 「每镜头一段」。未被至少一条草稿引用的基础镜头按原顺序追加，保证「不丢片」。
  function applyDraft(clips) {
    if (!clips || !clips.length) return 0;

    var baseByShot = {};
    state.editClips.forEach(function (c) { baseByShot[c.shot_id] = c; });
    var covered = {};

    var applied = [];
    clips.forEach(function (raw) {
      var base = baseByShot[raw.shot_id];
      if (!base) return; // 草稿中的镜头在当前任务里已不存在 → 丢弃
      covered[raw.shot_id] = true;

      var clip = cloneClip(base);
      clip.clip_id = (raw.clip_id == null ? null : raw.clip_id);
      clip.enabled = (raw.enabled !== false);
      if (clip.trimmable) {
        var duration = clip.orig_duration;
        var start = Math.max(0, Number(raw.start) || 0);
        var end = (raw.end == null ? duration : Number(raw.end));
        if (end > duration) { end = duration; }
        if (start >= duration || end <= start) { start = 0; end = duration; }
        clip.start = start;
        clip.end = end;
      }
      applied.push(clip);
    });

    // 草稿未覆盖的基础镜头按原顺序追加到末尾（shot 维度，split 只记一次）
    state.editClips.forEach(function (c) { if (!covered[c.shot_id]) { applied.push(c); } });

    if (!applied.length) return 0;
    state.editClips = applied;
    return applied.length;
  }

  /* --------------------------- 时间轴渲染 --------------------------- */
  function renderTimeline() {
    var track = $("editList");
    track.innerHTML = "";
    state.clipLayout = [];

    if (!state.editClips.length) {
      track.innerHTML = '<div class="empty">尚无片段</div>';
      renderRuler();
      updatePlayhead();
      updateSplitButton();
      return;
    }

    var left = 0;
    state.editClips.forEach(function (clip, index) {
      var width = clipWidth(clip);
      var el = buildClipEl(clip, index, width);
      track.appendChild(el);
      state.clipLayout.push({ index: index, left: left, width: width });
      left += width + TRACK_GAP;
    });

    renderRuler();
    updatePlayhead();
    updateSplitButton();
  }

  function buildClipEl(clip, index, width) {
    var el = document.createElement("div");
    el.className = "clip" + (clip.enabled ? "" : " disabled") +
      (state.selectedClip === index ? " selected" : "");
    el.dataset.index = String(index);
    el.dataset.clipId = effectiveId(clip);
    el.style.width = width + "px";
    el.draggable = true;
    el.addEventListener("dragstart", function (ev) { onClipDragStart(ev, index, el); });
    el.addEventListener("dragover", onCardDragOver);
    el.addEventListener("dragleave", onCardDragLeave);
    el.addEventListener("drop", onCardDrop);

    // 左裁剪手柄
    var leftH = document.createElement("span");
    leftH.className = "clip-handle left";
    leftH.dataset.handle = "left";
    leftH.title = "拖动裁剪起点";
    leftH.addEventListener("pointerdown", function (ev) { beginTrim(ev, index, "left", el); });
    el.appendChild(leftH);

    // 片段主体
    var body = document.createElement("div");
    body.className = "clip-body";

    var toggle = document.createElement("button");
    toggle.className = "clip-toggle";
    toggle.type = "button";
    toggle.textContent = clip.enabled ? "✓" : "⊘";
    toggle.title = clip.enabled ? "点击禁用该片段" : "点击启用该片段";
    toggle.onpointerdown = function (ev) { ev.stopPropagation(); };
    toggle.onclick = function (ev) { ev.stopPropagation(); setClipEnabled(index, !clip.enabled); };
    body.appendChild(toggle);

    if (clip.frame_path) {
      var img = document.createElement("img");
      img.className = "clip-thumb";
      img.alt = clip.shot_id + " 末帧";
      img.loading = "lazy";
      img.draggable = false;
      img.src = "/api/pipeline/" + state.taskId + "/frames/" + clip.frame_path.split("/").pop();
      body.appendChild(img);
    } else {
      var ph = document.createElement("div");
      ph.className = "clip-thumb empty-thumb";
      body.appendChild(ph);
    }

    var label = document.createElement("span");
    label.className = "clip-label";
    label.textContent = "#" + (index + 1) + " " + clip.shot_id +
      (clip.clip_id ? " ·" + clip.clip_id : "");
    label.title = label.textContent + "（" + clipDuration(clip).toFixed(1) + "s）";
    body.appendChild(label);

    el.appendChild(body);

    // 右裁剪手柄
    var rightH = document.createElement("span");
    rightH.className = "clip-handle right";
    rightH.dataset.handle = "right";
    rightH.title = "拖动裁剪终点";
    rightH.addEventListener("pointerdown", function (ev) { beginTrim(ev, index, "right", el); });
    el.appendChild(rightH);

    return el;
  }

  function renderRuler() {
    var ruler = $("timelineRuler");
    ruler.innerHTML = "";
    if (!state.editClips.length) { ruler.style.width = "0px"; return; }

    var totalW = 0;
    state.clipLayout.forEach(function (L) { totalW = Math.max(totalW, L.left + L.width); });
    ruler.style.width = totalW + "px";

    var cum = 0;
    state.editClips.forEach(function (clip, i) {
      var L = state.clipLayout[i];
      if (!L) return;
      var tick = document.createElement("span");
      tick.className = "ruler-tick";
      tick.style.left = L.left + "px";
      tick.textContent = fmtTime(cum);
      ruler.appendChild(tick);
      cum += clipDuration(clip);
    });
    var endTick = document.createElement("span");
    endTick.className = "ruler-tick end";
    endTick.style.left = totalW + "px";
    endTick.textContent = fmtTime(cum);
    ruler.appendChild(endTick);
  }

  function updatePlayhead() {
    var ph = $("timelinePlayhead");
    if (!state.editClips.length) { ph.classList.add("hidden"); return; }
    ph.classList.remove("hidden");
    ph.style.left = Math.max(0, state.playheadX || 0) + "px";
  }

  // 命中测试：x（轨道内容坐标）→ 落在哪个片段上
  function hitTest(x) {
    var layout = state.clipLayout;
    if (!layout.length) return null;
    for (var i = 0; i < layout.length; i++) {
      if (x >= layout[i].left && x <= layout[i].left + layout[i].width) { return layout[i]; }
    }
    var first = layout[0];
    var last = layout[layout.length - 1];
    if (x < first.left) return first;
    if (x > last.left + last.width) return last;
    return null;
  }

  function updateSplitButton() {
    var ok = false;
    if (state.selectedClip != null && state.clipLayout[state.selectedClip] &&
        state.editClips[state.selectedClip]) {
      var hit = state.clipLayout[state.selectedClip];
      var local = ((state.playheadX || 0) - hit.left) / pxPerSec();
      var dur = clipDuration(state.editClips[state.selectedClip]);
      ok = (local > 0.05 && local < dur - 0.05);
    }
    $("splitClipBtn").disabled = !ok;
  }

  // 单击轨道 / 刻度尺 → 命中片段，选中并把播放头定位到该处（AC21）
  function onTrackClick(ev) {
    if (!state.editClips.length) return;
    var target = ev.target;
    if (target.classList && target.classList.contains("clip-handle")) return;
    if (target.closest && target.closest(".clip-toggle")) return;

    var rect = this.getBoundingClientRect();
    var x = ev.clientX - rect.left;
    var hit = hitTest(x);
    if (!hit) return;

    state.selectedClip = hit.index;
    state.playheadX = Math.max(hit.left, Math.min(x, hit.left + hit.width));

    var clip = state.editClips[hit.index];
    if (clip && clip.enabled) {
      var local = (state.playheadX - hit.left) / pxPerSec();
      var player = $("previewPlayer");
      stopEditPlayback();
      player.src = "/api/pipeline/" + state.taskId + "/video/" + clip.video_path;
      player.onloadedmetadata = function () {
        try { player.currentTime = (Number(clip.start) || 0) + local; } catch (e) { /* ignore */ }
      };
    }
    renderTimeline();
  }

  /* --------------------------- 双臂裁剪（指针拖拽） --------------------------- */
  function beginTrim(ev, index, handle, clipEl) {
    ev.preventDefault();
    ev.stopPropagation();
    var clip = state.editClips[index];
    if (!clip) return;

    var startX = ev.clientX;
    var origStart = Number(clip.start) || 0;
    var origEnd = (clip.end == null ? clip.orig_duration : Number(clip.end));
    var duration = clip.orig_duration;
    clipEl.draggable = false; // 裁剪期间禁用拖拽排序，避免冲突

    function onMove(me) {
      var deltaSec = (me.clientX - startX) / pxPerSec();
      if (handle === "left") {
        var ns = Math.max(0, Math.min(origStart + deltaSec, duration));
        if (ns > origEnd - 0.1) { ns = Math.max(0, origEnd - 0.1); }
        clip.start = ns;
      } else {
        var ne = Math.max(0, Math.min(origEnd + deltaSec, duration));
        if (ne < (Number(clip.start) || 0) + 0.1) { ne = Math.min(duration, (Number(clip.start) || 0) + 0.1); }
        clip.end = ne;
      }
      clipEl.style.width = clipWidth(clip) + "px";
    }

    function onUp() {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      clipEl.draggable = true;
      // 归一：非法区间回退整段
      var s = Number(clip.start) || 0;
      var e = (clip.end == null ? duration : Number(clip.end));
      if (e > duration) e = duration;
      if (s >= duration || e <= s) { clip.start = 0; clip.end = duration; }
      else { clip.start = s; clip.end = e; }
      stopEditPlayback();
      renderTimeline();
      updateEditSummary();
      scheduleDraftSave();
    }

    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  }

  /* --------------------------- 分割（一刀两段，AC25） --------------------------- */
  function splitClip() {
    var index = state.selectedClip;
    if (index == null || !state.editClips[index] || !state.clipLayout[index]) {
      toast("请先在时间轴上选中一个片段", "err");
      return;
    }
    var hit = state.clipLayout[index];
    var clip = state.editClips[index];
    var local = ((state.playheadX || 0) - hit.left) / pxPerSec();
    var dur = clipDuration(clip);
    if (local <= 0.05 || local >= dur - 0.05) {
      toast("把播放头落在片段内部再分割", "err");
      return;
    }

    var parentId = effectiveId(clip);
    var absStart = Number(clip.start) || 0;
    var absEnd = (clip.end == null ? clip.orig_duration : Number(clip.end));
    var splitAt = absStart + local;

    var leftClip = cloneClip(clip);
    var rightClip = cloneClip(clip);
    leftClip.clip_id = parentId + "#1";
    rightClip.clip_id = parentId + "#2";
    leftClip.start = absStart;
    leftClip.end = splitAt;
    rightClip.start = splitAt;
    rightClip.end = absEnd;

    state.editClips.splice(index, 1, leftClip, rightClip);
    state.selectedClip = index;
    renderTimeline();
    updateEditSummary();
    scheduleDraftSave();
    toast("已在播放头处分割为两段", "ok");
  }

  function setClipEnabled(index, enabled) {
    var clip = state.editClips[index];
    if (!clip) return;
    clip.enabled = !!enabled;
    stopEditPlayback();
    renderTimeline();
    updateEditSummary();
    scheduleDraftSave();
  }

  /* --------------------------- 拖拽排序 --------------------------- */
  // 原生 HTML5 拖放，零依赖；拖源是整张片段块，放置目标是相邻片段块。
  var dragState = { index: null };

  function onClipDragStart(ev, index, el) {
    dragState.index = index;
    el.classList.add("dragging");
    try {
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", String(index));
      if (ev.dataTransfer.setDragImage) { ev.dataTransfer.setDragImage(el, 20, 20); }
    } catch (e) { /* 某些浏览器对 dataTransfer 有限制，忽略即可 */ }
  }

  function onCardDragOver(ev) {
    if (dragState.index === null) return;
    ev.preventDefault(); // 不 preventDefault 就不会触发 drop
    try { ev.dataTransfer.dropEffect = "move"; } catch (e) { /* ignore */ }
    this.classList.add("drag-over");
  }

  function onCardDragLeave() {
    this.classList.remove("drag-over");
  }

  function onCardDrop(ev) {
    if (dragState.index === null) return;
    ev.preventDefault();
    ev.stopPropagation();

    var target = parseInt(this.dataset.index, 10);
    var from = dragState.index;
    dragState.index = null;
    if (isNaN(target) || target === from) { clearDragMarks(); return; }

    var moved = state.editClips.splice(from, 1)[0];
    state.editClips.splice(target, 0, moved);
    stopEditPlayback();
    renderTimeline();
    updateEditSummary();
    scheduleDraftSave();
    toast("已把 " + moved.shot_id + " 移到第 " + (target + 1) + " 位", "info");
  }

  function clearDragMarks() {
    dragState.index = null;
    var list = $("editList");
    Array.prototype.forEach.call(list.querySelectorAll(".dragging, .drag-over"), function (el) {
      el.classList.remove("dragging");
      el.classList.remove("drag-over");
    });
  }

  /* --------------------------- 剪辑预览（顺序播放 + 播放头联动） --------------------------- */
  var editPlay = { active: false, index: 0 };

  function stopEditPlayback() {
    editPlay.active = false;
    var player = $("previewPlayer");
    player.ontimeupdate = null;
    player.onloadedmetadata = null;
    if (player && !player.paused) { player.pause(); }
  }

  // 返回 [{clip, index}]（保留在 state.editClips 中的真实索引，供播放头像素定位）
  function enabledClips() {
    var out = [];
    state.editClips.forEach(function (c, i) { if (c.enabled) { out.push({ clip: c, index: i }); } });
    return out;
  }

  function playEditSequence() {
    var entries = enabledClips();
    if (!entries.length) { toast("没有启用的片段", "err"); return; }
    editPlay.active = true;
    playClipAt(0);
  }

  function playClipAt(entryIndex) {
    var entries = enabledClips();
    if (!editPlay.active) return;
    if (entryIndex >= entries.length) { editPlay.active = false; return; }
    editPlay.index = entryIndex;

    var entry = entries[entryIndex];
    var clip = entry.clip;
    var player = $("previewPlayer");
    var startTime = Number(clip.start) || 0;
    var endTime = (clip.end == null ? clip.orig_duration : Number(clip.end));

    player.src = "/api/pipeline/" + state.taskId + "/video/" + clip.video_path;
    player.onloadedmetadata = function () {
      try { player.currentTime = startTime; } catch (e) { /* ignore */ }
      player.play().catch(function () { /* 自动播放可能被拦截 */ });
    };
    player.ontimeupdate = function () {
      // 播放头随当前片段播放移动（像素坐标 = 段左边界 + 段内已播时长 × pxPerSec）
      var L = state.clipLayout[entry.index];
      if (L) {
        state.playheadX = L.left + (player.currentTime - startTime) * pxPerSec();
        updatePlayhead();
      }
      if (player.currentTime >= endTime - 0.03) {
        player.ontimeupdate = null;
        player.onloadedmetadata = null;
        player.pause();
        playClipAt(entryIndex + 1);
      }
    };
  }

  /* --------------------------- 导出成片 --------------------------- */
  function resetExportResult() {
    var box = $("exportResult");
    box.classList.add("hidden");
    box.innerHTML = "";
  }

  function renderExportResult(data) {
    var box = $("exportResult");
    box.classList.remove("hidden");
    box.innerHTML = "";

    var title = document.createElement("div");
    title.className = "sub-title";
    title.textContent = "导出结果：" + (data.export_path || "");
    box.appendChild(title);

    var player = document.createElement("video");
    player.className = "video-player";
    player.controls = true;
    player.preload = "metadata";
    player.src = "/api/pipeline/" + state.taskId + "/video/" + data.export_path;
    box.appendChild(player);

    var row = document.createElement("div");
    row.className = "actions-row";

    var dl = document.createElement("a");
    dl.className = "btn btn-mini btn-ghost";
    dl.textContent = "下载导出成片";
    dl.href = data.download_url ||
      ("/api/pipeline/" + state.taskId + "/download-artifact?name=" + encodeURIComponent(data.export_path));
    row.appendChild(dl);

    if (data.edit_plan_path) {
      var plan = document.createElement("a");
      plan.className = "btn btn-mini btn-ghost";
      plan.textContent = "下载剪辑方案 (edit_plan)";
      plan.href = "/api/pipeline/" + state.taskId + "/download-artifact?name=" +
        encodeURIComponent(data.edit_plan_path);
      row.appendChild(plan);
    }

    var meta = document.createElement("span");
    meta.className = "hint";
    meta.textContent = "共 " + (data.clip_count || 0) + " 个片段 · 时长 " +
      Number(data.total_duration || 0).toFixed(1) + "s";
    row.appendChild(meta);
    box.appendChild(row);
  }

  async function exportEdit() {
    if (!state.taskId || !state.editClips.length) return;
    var clips = currentEdl();
    if (!clips.some(function (c) { return c.enabled; })) {
      toast("请至少启用一个片段", "err");
      return;
    }
    stopEditPlayback();
    // 导出前先把方案落盘，保证「导出的成片」与「可复现的方案」始终一致
    await saveDraft(true);
    $("exportBtn").disabled = true;
    $("editExportHint").textContent = "导出中…（同步执行，请稍候）";
    try {
      var res = await apiPost("/api/pipeline/" + state.taskId + "/export", { clips: clips });
      if (!res.ok) {
        toast(res.error || "导出失败", "err");
        $("editExportHint").textContent = "";
        return;
      }
      state.exported = res.data;
      renderExportResult(res.data);
      $("editExportHint").textContent = "已导出 " + (res.data.clip_count || 0) + " 个片段";
      toast("导出成功：" + res.data.export_path, "ok");
    } catch (e) {
      toast("导出失败：" + e.message, "err");
      $("editExportHint").textContent = "";
    } finally {
      $("exportBtn").disabled = false;
    }
  }

  /* --------------------------- 渲染：评估报告 --------------------------- */
  function renderReport(report) {
    var body = $("reportBody");
    body.innerHTML = "";
    if (!report) { body.innerHTML = '<div class="empty">尚无评估报告</div>'; return; }

    body.appendChild(buildScriptBlock(report.script));
    if (report.video_skipped || !report.video) {
      var skip = document.createElement("div");
      skip.className = "empty";
      skip.textContent = "视频评估已跳过" + (report.video_skipped ? "（按配置跳过或评估失败降级）" : "");
      body.appendChild(skip);
    } else {
      body.appendChild(buildVideoBlock(report.video));
    }
    if (report.comment) {
      var c = document.createElement("div");
      c.className = "comment";
      c.textContent = "评语：" + report.comment;
      body.appendChild(c);
    }
  }

  function scoreBlock(title, total, rows) {
    var block = document.createElement("div");
    block.className = "score-block";
    var head = document.createElement("div");
    head.className = "score-head";
    head.innerHTML = '<span class="score-title">' + title + '</span>' +
      '<span class="score-total">' + (total != null ? Number(total).toFixed(1) : "—") + ' / 10</span>';
    block.appendChild(head);
    rows.forEach(function (row) {
      var barRow = document.createElement("div");
      barRow.className = "bar-row";
      var pct = Math.max(0, Math.min(100, (Number(row.value) || 0) * 10));
      barRow.innerHTML =
        '<span>' + row.label + '</span>' +
        '<span class="bar ' + (row.green ? "green" : "") + '"><span style="width:' + pct + '%"></span></span>' +
        '<span class="bar-value">' + (Number(row.value) || 0).toFixed(1) + '</span>';
      block.appendChild(barRow);
      if (row.comment) {
        var cm = document.createElement("div");
        cm.className = "hint";
        cm.style.margin = "-2px 0 8px 150px";
        cm.textContent = row.comment;
        block.appendChild(cm);
      }
    });
    return block;
  }

  function buildScriptBlock(score) {
    score = score || {};
    var comments = score.comments || {};
    return scoreBlock("脚本质量", score.total, [
      { label: "连贯性", value: score.coherence, comment: comments.coherence },
      { label: "可拍性", value: score.shootability, comment: comments.shootability },
      { label: "角色一致性", value: score.character_consistency, comment: comments.character_consistency },
      { label: "剧情完整度", value: score.plot_completeness, comment: comments.plot_completeness }
    ]);
  }

  function buildVideoBlock(score) {
    score = score || {};
    var comments = score.comments || {};
    return scoreBlock("视频保真度", score.total, [
      { label: "画面一致性", value: score.visual_consistency, comment: comments.visual_consistency, green: true },
      { label: "对白匹配", value: score.dialogue_match, comment: comments.dialogue_match, green: true },
      { label: "风格符合度", value: score.style_fidelity, comment: comments.style_fidelity, green: true }
    ]);
  }

  /* --------------------------- 剧情编辑 / 重新生成分镜 --------------------------- */
  function fillStoryForm(story) {
    story = story || {};
    $("storyTitleInput").value = story.title || "";
    $("storyGenreInput").value = story.genre || "";
    $("storyLoglineInput").value = story.logline || "";
    $("storySettingInput").value = story.setting || "";
    $("storySynopsisInput").value = story.synopsis || "";
    $("storyJson").textContent = JSON.stringify(story, null, 2);

    var chars = $("storyCharsView");
    chars.innerHTML = "";
    var charList = story.characters || [];
    if (charList.length) {
      charList.forEach(function (c) {
        var line = document.createElement("span");
        line.className = "ro-item";
        line.textContent = "· " + (c.name || "（未命名）") + (c.desc ? "：" + c.desc : "");
        chars.appendChild(line);
      });
    } else {
      chars.innerHTML = '<span class="ro-empty">（无）</span>';
    }

    var beats = $("storyBeatsView");
    beats.innerHTML = "";
    var beatList = story.beats || [];
    if (beatList.length) {
      beatList.forEach(function (b, i) {
        var line = document.createElement("span");
        line.className = "ro-item";
        line.textContent = "· " + (b.index || i + 1) + ". " + (b.summary || "") +
          (b.purpose ? "（" + b.purpose + "）" : "");
        beats.appendChild(line);
      });
    } else {
      beats.innerHTML = '<span class="ro-empty">（无）</span>';
    }
  }

  function clearStoryForm() {
    ["storyTitleInput", "storyGenreInput", "storyLoglineInput",
     "storySettingInput", "storySynopsisInput"].forEach(function (id) { $(id).value = ""; });
    $("storyJson").textContent = "（尚未生成）";
    $("storyCharsView").innerHTML = '<span class="ro-empty">（无）</span>';
    $("storyBeatsView").innerHTML = '<span class="ro-empty">（无）</span>';
    $("storyRerunHint").textContent = "";
  }

  function buildStoryFromForm() {
    var base = state.story || {};
    return {
      title: $("storyTitleInput").value.trim() || "未命名故事",
      logline: $("storyLoglineInput").value.trim(),
      synopsis: $("storySynopsisInput").value.trim(),
      setting: $("storySettingInput").value.trim(),
      characters: base.characters || [],
      beats: base.beats || [],
      genre: $("storyGenreInput").value.trim(),
      theme: base.theme || "",
      source_dialogue: base.source_dialogue || "",
      extra: base.extra || {}
    };
  }

  async function rerunFromStory() {
    if (!state.story) { toast("尚无剧情可编辑，请先生成一次", "err"); return; }

    var edited = buildStoryFromForm();
    if (!edited.synopsis) {
      toast("「完整故事情节」不能为空", "err");
      $("storySynopsisInput").focus();
      return;
    }

    var styleText = $("styleSelect").options[$("styleSelect").selectedIndex].text;
    if (!window.confirm(
      "将新建一个任务，用你修改后的剧情直接生成分镜（跳过剧情补全阶段）。\n\n" +
      "镜头数：" + $("shotCountSelect").value + " 个　每镜头：" + $("shotDurationSelect").value + " 秒\n" +
      "风格：" + styleText + "　分辨率：" + $("resolutionSelect").value + "\n\n" +
      "当前任务及其产物不受影响。确认继续？"
    )) { return; }

    // dialogue 仅用于溯源，后端要求非空（min_length=1）
    var dialogue = $("dialogue").value.trim() || edited.source_dialogue ||
      edited.title || "（由编辑后的剧情重新生成）";

    var payload = {
      dialogue: dialogue,
      style: $("styleSelect").value,
      shot_count: parseInt($("shotCountSelect").value, 10),
      shot_duration: parseInt($("shotDurationSelect").value, 10),
      resolution: $("resolutionSelect").value,
      skip_video_eval: $("skipVideoEval").checked,
      story: edited,
      skip_story: false
    };

    closeModal("storyModal");
    stopPolling();
    stopEditPlayback();
    state.taskId = null;
    resetProgressUI();
    $("runBtn").disabled = true;
    appendLogLine("正在用编辑后的剧情创建新任务…");

    try {
      var res = await apiPost("/api/pipeline/run", payload);
      if (!res.ok) {
        toast(res.error || "启动失败", "err");
        $("runBtn").disabled = false;
        return;
      }
      state.taskId = res.data.task_id;
      rememberTask(state.taskId);
      toast("已用编辑后的剧情启动新任务：" + state.taskId, "ok");
      startPolling();
    } catch (e) {
      toast("启动失败：" + e.message, "err");
      $("runBtn").disabled = false;
    }
  }

  /* --------------------------- 分镜确认区（人审门） --------------------------- */
  async function enterReviewMode(status) {
    state.reviewActive = true;
    $("section-review").classList.remove("hidden");
    $("reviewStateChip").textContent = "⏸ 待确认";
    $("reviewStateChip").className = "chip warn";
    $("reviewHint").textContent = "";

    // 拉取完整剧情与脚本（挂起态下无镜头视频，只有文本）
    try {
      var sres = await apiGet("/api/pipeline/" + state.taskId + "/story");
      if (sres.ok) { state.story = sres.data; }
    } catch (e) { /* ignore */ }
    if (status && status.script) { state.script = status.script; }
    try {
      var cres = await apiGet("/api/pipeline/" + state.taskId + "/script");
      if (cres.ok) {
        state.script = cres.data;
        $("scriptJson").textContent = JSON.stringify(cres.data, null, 2);
      }
    } catch (e) { /* ignore */ }

    fillReviewStory(state.story || {});
    renderReviewShots(state.script || {});
    document.getElementById("section-review").scrollIntoView({ behavior: "smooth" });
    toast("分镜已生成：请在「分镜确认区」核对/修改后确认生成视频", "info");
  }

  function exitReviewMode() {
    state.reviewActive = false;
    $("section-review").classList.add("hidden");
  }

  function clearReviewForm() {
    ["reviewTitle", "reviewGenre", "reviewLogline",
     "reviewSetting", "reviewSynopsis"].forEach(function (id) { $(id).value = ""; });
    $("reviewCharsView").innerHTML = '<span class="ro-empty">（无）</span>';
    $("reviewBeatsView").innerHTML = '<span class="ro-empty">（无）</span>';
    $("reviewShotList").innerHTML = "";
    $("reviewHint").textContent = "";
  }

  function fillReviewStory(story) {
    story = story || {};
    $("reviewTitle").value = story.title || "";
    $("reviewGenre").value = story.genre || "";
    $("reviewLogline").value = story.logline || "";
    $("reviewSetting").value = story.setting || "";
    $("reviewSynopsis").value = story.synopsis || "";

    var chars = $("reviewCharsView");
    chars.innerHTML = "";
    var charList = story.characters || [];
    if (charList.length) {
      charList.forEach(function (c) {
        var line = document.createElement("span");
        line.className = "ro-item";
        line.textContent = "· " + (c.name || "（未命名）") + (c.desc ? "：" + c.desc : "");
        chars.appendChild(line);
      });
    } else {
      chars.innerHTML = '<span class="ro-empty">（无）</span>';
    }

    var beats = $("reviewBeatsView");
    beats.innerHTML = "";
    var beatList = story.beats || [];
    if (beatList.length) {
      beatList.forEach(function (b, i) {
        var line = document.createElement("span");
        line.className = "ro-item";
        line.textContent = "· " + (b.index || i + 1) + ". " + (b.summary || "") +
          (b.purpose ? "（" + b.purpose + "）" : "");
        beats.appendChild(line);
      });
    } else {
      beats.innerHTML = '<span class="ro-empty">（无）</span>';
    }
  }

  function reviewField(labelText, field, value, isTextarea) {
    var wrap = document.createElement("label");
    wrap.className = "field";
    var span = document.createElement("span");
    span.className = "field-label";
    span.textContent = labelText;
    wrap.appendChild(span);
    var input;
    if (isTextarea) {
      input = document.createElement("textarea");
      input.rows = 2;
    } else {
      input = document.createElement("input");
      input.type = "text";
    }
    input.dataset.field = field;
    input.value = value || "";
    wrap.appendChild(input);
    return wrap;
  }

  function renderReviewShots(script) {
    var box = $("reviewShotList");
    box.innerHTML = "";
    var shots = (script && script.shots) || [];
    if (!shots.length) { box.innerHTML = '<div class="empty">尚无分镜</div>'; return; }

    shots.forEach(function (shot) {
      var card = document.createElement("div");
      card.className = "review-shot";
      card.dataset.shotId = shot.shot_id || "";

      var head = document.createElement("div");
      head.className = "review-shot-head";
      var name = document.createElement("span");
      name.className = "review-shot-id";
      name.textContent = shot.shot_id || "(未命名)";
      head.appendChild(name);

      var durLabel = document.createElement("label");
      durLabel.className = "inline-input";
      durLabel.appendChild(document.createTextNode("时长"));
      var dur = document.createElement("input");
      dur.type = "number";
      dur.min = "2";
      dur.max = "10";
      dur.step = "1";
      dur.className = "review-dur";
      dur.dataset.field = "duration";
      dur.value = String(shot.duration || 4);
      durLabel.appendChild(dur);
      durLabel.appendChild(document.createTextNode("秒"));
      head.appendChild(durLabel);
      card.appendChild(head);

      card.appendChild(reviewField("分镜提示词 prompt", "prompt", shot.prompt, true));
      card.appendChild(reviewField("动作 action", "action", shot.action, false));
      card.appendChild(reviewField("运镜 camera", "camera", shot.camera, false));
      card.appendChild(reviewField("对白 dialogue", "dialogue", shot.dialogue, false));

      box.appendChild(card);
    });
  }

  function buildStoryFromReview() {
    var base = state.story || {};
    return {
      title: $("reviewTitle").value.trim() || "未命名故事",
      logline: $("reviewLogline").value.trim(),
      synopsis: $("reviewSynopsis").value.trim(),
      setting: $("reviewSetting").value.trim(),
      characters: base.characters || [],
      beats: base.beats || [],
      genre: $("reviewGenre").value.trim(),
      theme: base.theme || "",
      source_dialogue: base.source_dialogue || "",
      extra: base.extra || {}
    };
  }

  function buildScriptFromReview() {
    var base = state.script || {};
    var shots = [];
    var cards = $("reviewShotList").querySelectorAll(".review-shot");
    Array.prototype.forEach.call(cards, function (card) {
      var get = function (field) {
        var el = card.querySelector('[data-field="' + field + '"]');
        return el ? el.value : "";
      };
      var durRaw = parseInt(get("duration"), 10);
      shots.push({
        shot_id: card.dataset.shotId || "",
        prompt: get("prompt").trim(),
        action: get("action"),
        camera: get("camera"),
        dialogue: get("dialogue"),
        duration: (isNaN(durRaw) ? 0 : durRaw)
      });
    });
    return {
      title: base.title || "未命名短剧",
      style: base.style || $("styleSelect").value,
      characters: base.characters || [],
      scene: base.scene || "",
      positions: base.positions || [],
      shots: shots
    };
  }

  async function confirmReview() {
    if (!state.taskId) return;
    var story = buildStoryFromReview();
    var script = buildScriptFromReview();

    if (!story.synopsis) {
      toast("「完整故事情节」不能为空", "err");
      $("reviewSynopsis").focus();
      return;
    }
    for (var i = 0; i < script.shots.length; i++) {
      var s = script.shots[i];
      var label = s.shot_id || ("#" + (i + 1));
      if (s.duration < 2 || s.duration > 10) {
        $("reviewHint").textContent = label + " 的时长须为 2–10 秒";
        toast($("reviewHint").textContent, "err");
        return;
      }
      if (!s.prompt) {
        $("reviewHint").textContent = label + " 的分镜提示词不能为空";
        toast($("reviewHint").textContent, "err");
        return;
      }
    }

    $("reviewConfirmBtn").disabled = true;
    $("reviewSkipBtn").disabled = true;
    $("reviewHint").textContent = "正在提交确认…";
    try {
      var res = await apiPost("/api/pipeline/" + state.taskId + "/confirm-script",
        { story: story, script: script });
      if (!res.ok) {
        $("reviewHint").textContent = "";
        toast(res.error || "确认失败", "err");
        return;
      }
      var rerun = !!res.data.rerun_script;
      toast("已确认" + (rerun ? "（剧情有改动，将重跑脚本）" : "") + "，开始生成视频", "ok");
      appendLogLine(rerun ? "已确认：剧情有改动，重跑脚本后继续生成视频…" : "已确认分镜，继续生成视频…");
      exitReviewMode();
      $("runBtn").disabled = true;
      startPolling();
    } catch (e) {
      toast("确认失败：" + e.message, "err");
      $("reviewHint").textContent = "";
    } finally {
      $("reviewConfirmBtn").disabled = false;
      $("reviewSkipBtn").disabled = false;
    }
  }

  async function skipReview() {
    if (!state.taskId) return;
    $("reviewConfirmBtn").disabled = true;
    $("reviewSkipBtn").disabled = true;
    $("reviewHint").textContent = "正在跳过确认…";
    try {
      var res = await apiPost("/api/pipeline/" + state.taskId + "/confirm-script", {});
      if (!res.ok) {
        $("reviewHint").textContent = "";
        toast(res.error || "跳过失败", "err");
        return;
      }
      toast("已跳过确认，按当前分镜生成视频", "ok");
      appendLogLine("已跳过编辑，按当前分镜生成视频…");
      exitReviewMode();
      $("runBtn").disabled = true;
      startPolling();
    } catch (e) {
      toast("跳过失败：" + e.message, "err");
      $("reviewHint").textContent = "";
    } finally {
      $("reviewConfirmBtn").disabled = false;
      $("reviewSkipBtn").disabled = false;
    }
  }

  /* --------------------------- 弹窗 --------------------------- */
  function openModal(id) { $(id).classList.remove("hidden"); }
  function closeModal(id) { $(id).classList.add("hidden"); }

  /* --------------------------- 健康检查 --------------------------- */
  async function checkHealth() {
    try {
      var res = await apiGet("/api/health");
      if (!res.ok) return;
      var data = res.data || {};
      var port = data.port || location.port || 8000;
      $("addressChip").textContent = "● 已就绪 · http://127.0.0.1:" + port;
      if (!data.ffmpeg) {
        showBanner("error", "未检测到 FFmpeg，Mock 视频生成与视频拼接将不可用。" +
          " 请安装 FFmpeg 并加入 PATH（下载：https://www.gyan.dev/ffmpeg/builds/），完成后重启服务。");
      } else {
        showBanner("", "");
      }
    } catch (e) {
      showBanner("error", "无法连接后端服务：" + e.message);
    }
  }

  function showBanner(kind, text) {
    var el = $("banner");
    if (!text) { el.className = "banner hidden"; el.textContent = ""; return; }
    el.className = "banner " + kind;
    el.textContent = text;
  }

  /* --------------------------- 初始化 --------------------------- */
  function initExamples() {
    var select = $("exampleSelect");
    EXAMPLES.forEach(function (ex, index) {
      var opt = document.createElement("option");
      opt.value = String(index);
      opt.textContent = ex.name;
      select.appendChild(opt);
    });
  }

  function bindEvents() {
    document.querySelectorAll('input[name="llm_mode"]').forEach(function (el) {
      el.addEventListener("change", syncModeUI);
    });
    document.querySelectorAll('input[name="video_provider"]').forEach(function (el) {
      el.addEventListener("change", syncModeUI);
    });

    $("refreshModelsBtn").addEventListener("click", refreshOllamaModels);
    $("testLlmBtn").addEventListener("click", testLlm);
    $("testVideoBtn").addEventListener("click", testVideo);
    $("saveConfigBtn").addEventListener("click", function () { saveConfig(false); });
    $("chainStartBtn").addEventListener("click", chainStart);

    $("loadExampleBtn").addEventListener("click", function () {
      var idx = $("exampleSelect").value;
      if (idx === "") { toast("请先选择一个示例对话", "info"); return; }
      $("dialogue").value = EXAMPLES[parseInt(idx, 10)].text;
      $("inputHint").textContent = "";
    });

    $("runBtn").addEventListener("click", startPipeline);
    $("resetBtn").addEventListener("click", function () {
      stopPolling();
      state.taskId = null;
      rememberTask(null); // 解除「刷新后恢复」的绑定
      resetProgressUI();
      $("runBtn").disabled = false;
      $("viewScriptBtn").disabled = true;
      $("downloadFinalBtn").disabled = true;
      $("downloadReportBtn").disabled = true;
    });

    $("viewStoryBtn").addEventListener("click", function () {
      if (state.story) { fillStoryForm(state.story); }
      openModal("storyModal");
    });
    $("storyRerunBtn").addEventListener("click", rerunFromStory);
    $("reviewConfirmBtn").addEventListener("click", confirmReview);
    $("reviewSkipBtn").addEventListener("click", skipReview);
    $("storyDownloadBtn").addEventListener("click", function () {
      if (state.taskId) downloadArtifact("story.json");
    });

    $("viewScriptBtn").addEventListener("click", function () {
      if (state.script) {
        $("scriptJson").textContent = JSON.stringify(state.script, null, 2);
      }
      openModal("scriptModal");
    });
    $("scriptDownloadBtn").addEventListener("click", function () {
      if (state.taskId) downloadArtifact("script.json");
    });

    $("playEditBtn").addEventListener("click", playEditSequence);
    $("splitClipBtn").addEventListener("click", splitClip);
    $("editList").addEventListener("click", onTrackClick);
    $("timelineRuler").addEventListener("click", onTrackClick);
    $("exportBtn").addEventListener("click", exportEdit);
    $("resetEditBtn").addEventListener("click", function () {
      state.editClips = JSON.parse(JSON.stringify(state.editOriginal));
      state.selectedClip = null;
      state.playheadX = 0;
      stopEditPlayback();
      resetExportResult();
      $("editExportHint").textContent = "";
      renderTimeline();
      updateEditSummary();
      scheduleDraftSave();
      toast("已重置为原始剪辑", "info");
    });

    $("downloadFinalBtn").addEventListener("click", function () {
      if (state.taskId) window.location.href = "/api/pipeline/" + state.taskId + "/download";
    });
    $("downloadReportBtn").addEventListener("click", function () {
      if (state.taskId) downloadArtifact("evaluation_report.json");
    });

    $("helpBtn").addEventListener("click", function () { openModal("helpModal"); });
    $("helpCloseBtn").addEventListener("click", function () { closeModal("helpModal"); });

    document.querySelectorAll("[data-close]").forEach(function (el) {
      el.addEventListener("click", function () {
        closeModal("helpModal");
        closeModal("scriptModal");
        closeModal("storyModal");
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        closeModal("helpModal");
        closeModal("scriptModal");
        closeModal("storyModal");
      }
    });

    // 拖拽结束时统一清理高亮（挂 document 上，避免片段卡被重渲染后 dragend 丢失）
    document.addEventListener("dragend", clearDragMarks);
    document.addEventListener("drop", clearDragMarks);
  }

  function init() {
    fillRange($("shotCountSelect"), 2, 8, " 个");
    fillRange($("shotDurationSelect"), 2, 10, " 秒");
    $("shotCountSelect").value = "4";
    $("shotDurationSelect").value = "4";
    initExamples();
    clearStoryForm();
    bindEvents();
    syncModeUI();
    loadConfig();
    checkHealth();
    restoreSession(); // 刷新后恢复上次任务与剪辑方案
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
