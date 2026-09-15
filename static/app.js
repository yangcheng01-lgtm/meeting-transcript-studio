const state = { project: null, jobPoller: null, previewAudio: null, summarySkills: [] };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[c]));

function api(path, options = {}) {
  return fetch(path, options).then(async (response) => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `请求失败：${response.status}`);
    return body;
  });
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast ${error ? "error" : ""}`;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => element.classList.add("hidden"), 5200);
}

function time(seconds) {
  seconds = Math.max(0, Number(seconds || 0));
  const hour = Math.floor(seconds / 3600);
  const minute = Math.floor((seconds % 3600) / 60);
  const second = seconds % 60;
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}:${second.toFixed(2).padStart(5, "0")}`;
}

function speakerInfo(speaker) {
  return state.project?.speaker_map?.[speaker] || { name: speaker, role: "", color: "#777" };
}

function speakerLabel(speaker) {
  const info = speakerInfo(speaker);
  return info.role ? `${info.name}（${info.role}）` : info.name;
}

function unresolvedSpeakers(project) {
  const referenced = new Set((project?.transcript_segments || []).map((item) => item.speaker).filter((speaker) => speaker && speaker !== "UNKNOWN"));
  return [...referenced].filter((speaker) => {
    const name = String(project?.speaker_map?.[speaker]?.name || "").trim();
    return !name || name === speaker || /^SPEAKER[_\s-]*\d+$/i.test(name) || /^发言人\d+$/.test(name);
  });
}

function selectedSpeakers(speaker) {
  const speakers = Object.keys(state.project?.speaker_map || {});
  return ["UNKNOWN", ...speakers.filter((item) => item !== "UNKNOWN")]
    .map((item) => `<option value="${escapeHtml(item)}" ${speaker === item ? "selected" : ""}>${escapeHtml(speakerLabel(item))}</option>`).join("");
}

function renderProject() {
  const p = state.project;
  if (!p) return;
  $("#projectStatus").classList.remove("status-highlight");
  const hasDiar = Boolean(p.diarization_segments?.length);
  const hasTranscript = Boolean(p.transcript_segments?.length);
  const missingNames = unresolvedSpeakers(p);
  $("#emptyState").classList.add("hidden");
  $("#studio").classList.remove("hidden");
  $("#projectTitle").textContent = p.title;
  $("#projectMeta").textContent = `本地项目 · ${p.id.toUpperCase()}`;
  $("#mediaName").textContent = p.source_count > 1 ? `${p.source_count} 个源文件 · 已合并时间轴` : (p.media_path?.split(/[\/]/).pop() || "—");
  const media = $("#audioPlayer");
  const wantedSrc = `${location.origin}/api/projects/${p.id}/media`;
  if (media.src !== wantedSrc) { media.src = wantedSrc; media.load(); }
  const speakerTotal = Object.keys(p.speaker_map || {}).length;
  $("#speakerCount").textContent = `${speakerTotal} 位说话人`;
  const speakerSummary = $("#detectedSpeakerCount");
  if (speakerSummary) speakerSummary.textContent = `系统识别到 ${speakerTotal} 位说话人`;
  $("#diarStatus").textContent = hasDiar ? `${p.diarization_segments.length} 个时间片段` : "尚未运行";
  $("#asrStatus").textContent = p.asr_segments?.length ? `${p.asr_segments.length} 个文字片段 · ${p.asr_timing_quality === "coarse" ? "不可可靠对齐" : "可对齐"}` : "等待 qwen3-asr";
  $("#alignStatus").textContent = hasTranscript ? `${p.transcript_segments.length} 段已对齐` : "等待结果";
  $("#projectStatus").textContent = hasTranscript ? (missingNames.length ? "待填写人名" : "可生成报告") : hasDiar ? "转写处理中 / 待转写" : "等待处理";
  $("#runPipelineBtn").textContent = hasTranscript ? "重新识别" : "开始识别";
  $("#simpleRecognizeStatus").textContent = hasTranscript ? "识别已完成，可重新运行" : hasDiar ? "Speaker 已完成，等待文字" : "导入后点击开始";
  $("#simpleNamingStatus").textContent = !hasDiar ? "等待识别完成" : missingNames.length ? `还需命名 ${missingNames.length} 位说话人` : "人名已确认";
  $("#simpleExportStatus").textContent = !hasTranscript ? "等待识别完成" : missingNames.length ? "请先填写全部人名" : "可导出";
  $("#simpleExportBtn").disabled = !hasTranscript || Boolean(missingNames.length);
  $("#generateMinutesBtn").disabled = !hasTranscript || Boolean(missingNames.length);
  $("#generateMinutesAdvancedBtn").disabled = !hasTranscript || Boolean(missingNames.length);
  const saveSpeakersBtn = $("#saveSpeakersBtn");
  saveSpeakersBtn.classList.toggle("action-highlight", hasDiar);
  $("#generateMinutesBtn").classList.toggle("action-highlight", hasTranscript && !missingNames.length);
  $("#referenceText").textContent = p.reference_text || p.asr_raw_text || "尚未导入纯文字稿。";
  if ($("#glossaryInput")) $("#glossaryInput").value = (p.glossary || []).join("\n");
  if (p.latest_report_skill && $("#summarySkillSelect")) $("#summarySkillSelect").value = p.latest_report_skill;
  $("#transcriptFold").open = false;
  renderSpeakers(); renderTimeline(); renderTranscript();
}


function speakerStats(speaker) {
  const diar = (state.project?.diarization_segments || []).filter((item) => item.speaker === speaker);
  const duration = diar.reduce((sum, item) => sum + Math.max(0, item.end - item.start), 0);
  const longest = diar.reduce((best, item) => !best || (item.end - item.start) > (best.end - best.start) ? item : best, null);
  const source = state.project?.transcript_segments?.length ? state.project.transcript_segments : [];
  const lines = source.filter((item) => item.speaker === speaker && item.text?.trim()).slice(0, 2).map((item) => item.text.trim());
  return { duration, longest, excerpt: lines.join(" ") || "暂未完成文字对齐；可先试听代表片段。" };
}

function renderSpeakers() {
  const list = $("#speakerList");
  const mapping = state.project.speaker_map || {};
  const orderedSpeakers = [...(state.project.diarization_segments || [])]
    .sort((a, b) => (a.start || 0) - (b.start || 0) || (a.end || 0) - (b.end || 0));
  const firstSeen = new Map();
  orderedSpeakers.forEach((item) => { if (!firstSeen.has(item.speaker)) firstSeen.set(item.speaker, firstSeen.size + 1); });
  const speakers = Object.keys(mapping).sort((a, b) => (firstSeen.get(a) || 9999) - (firstSeen.get(b) || 9999));
  list.innerHTML = speakers.map((id) => {
    const info = mapping[id];
    const stats = speakerStats(id);
    const cue = stats.longest ? `代表片段 ${time(stats.longest.start)} – ${time(stats.longest.end)}` : "暂无有效语音片段";
    const duration = stats.duration ? `累计 ${Math.round(stats.duration)} 秒` : "暂无时长";
    return `
      <article class="speaker-card" style="--speaker-color:${escapeHtml(info.color || "#777")}">
        <div class="speaker-head"><strong>${escapeHtml(speakerLabel(id))}</strong><span>${escapeHtml(duration)}</span></div>
        <label>姓名<input data-speaker="${escapeHtml(id)}" data-field="name" value="${escapeHtml(info.name || id)}"></label>
        <label>角色<input data-speaker="${escapeHtml(id)}" data-field="role" value="${escapeHtml(info.role || "")}" placeholder="例如：采访者"></label>
        <p class="speaker-excerpt">“${escapeHtml(stats.excerpt)}”</p>
        <div class="speaker-tools"><small>${escapeHtml(cue)}</small>${stats.longest ? `<button class="speaker-preview-btn" data-preview-speaker="${escapeHtml(id)}">试听代表片段</button>` : ""}</div>
      </article>`;
  }).join("") || `<p class="panel-note">还没有可标记的 Speaker。先运行或导入说话人分离结果。</p>`;
  list.querySelectorAll("[data-preview-speaker]").forEach((button) => button.addEventListener("click", () => playSpeakerPreview(button.dataset.previewSpeaker)));
}

async function playSpeakerPreview(speaker) {
  try {
    if (state.previewAudio) { state.previewAudio.pause(); state.previewAudio = null; }
    const audio = new Audio(`/api/projects/${state.project.id}/speaker-preview/${encodeURIComponent(speaker)}?v=${Date.now()}`);
    state.previewAudio = audio;
    audio.addEventListener("ended", () => { if (state.previewAudio === audio) state.previewAudio = null; });
    audio.addEventListener("error", () => toast("代表片段无法播放；请确认本地服务仍在运行。", true));
    await audio.play();
    toast(`正在试听 ${speakerLabel(speaker)} 的代表片段。`);
  } catch (error) { toast(`无法播放代表片段：${error.message || "浏览器拒绝播放"}`, true); }
}


function renderTimeline() {
  const timeline = $("#timeline");
  const segments = state.project.diarization_segments || [];
  const max = Math.max(...segments.map((x) => x.end), 0);
  $("#timelineEnd").textContent = max ? time(max) : "—";
  timeline.innerHTML = !max ? "" : segments.map((item) => {
    const info = speakerInfo(item.speaker);
    const left = (item.start / max) * 100;
    const width = Math.max(.25, ((item.end - item.start) / max) * 100);
    return `<div class="timeline-bar" title="${escapeHtml(speakerLabel(item.speaker))} · ${time(item.start)} – ${time(item.end)}" style="left:${left}%;width:${width}%;background:${escapeHtml(info.color || "#777")}"></div>`;
  }).join("");
}

function renderTranscript() {
  const list = $("#transcriptList");
  const segments = state.project.transcript_segments || [];
  $("#transcriptHeading").textContent = segments.length ? `已对齐 ${segments.length} 个文字片段` : "等待带时间戳的 ASR";
  $("#transcriptNotice").classList.toggle("hidden", Boolean(segments.length));
  if (!segments.length) {
    list.innerHTML = `<div class="notice">点击“开始识别”后，工具会自动生成带 Speaker 的逐字稿。完成后可在这里校对文本。</div>`;
    return;
  }
  list.innerHTML = segments.map((item, index) => `
    <article class="transcript-row" data-index="${index}">
      <div class="time-label">${time(item.start)}<br>↓ ${time(item.end)}<br><small>${escapeHtml(item.timing_quality || "segment")}</small></div>
      <select class="speaker-select" data-field="speaker">${selectedSpeakers(item.speaker)}</select>
      <textarea class="segment-text" data-field="text" aria-label="第 ${index + 1} 段文字">${escapeHtml(item.text)}</textarea>
    </article>`).join("");
}

async function refreshProject() {
  if (!state.project) return;
  state.project = await api(`/api/projects/${state.project.id}`);
  renderProject();
}

function renderJobProgress(job) {
  const panel = $("#jobProgress");
  if (!panel) return;
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const running = job.status === "running" || job.status === "queued";
  panel.classList.toggle("hidden", !running);
  if (!running) return;
  $("#jobProgressLabel").textContent = job.message || job.status;
  $("#jobProgressPercent").textContent = progress.toFixed(1) + "%";
  $("#jobProgressBar").style.width = progress + "%";
}

function jobStatus(job, onDone = refreshProject) {
  const jobId = job.id;
  const poll = async () => {
    try {
      const current = await api("/api/jobs/" + jobId);
      $("#projectStatus").textContent = current.message || current.status;
      $("#projectStatus").classList.add("status-highlight");
      renderJobProgress(current);
      if (current.status === "done") { $("#jobProgress").classList.add("hidden"); $("#projectStatus").classList.remove("status-highlight"); toast(current.message); await onDone(); return; }
      if (current.status === "error") { $("#jobProgress").classList.add("hidden"); $("#projectStatus").classList.remove("status-highlight"); toast(current.message, true); return; }
      window.setTimeout(poll, 1300);
    } catch (error) { toast(error.message, true); }
  };
  poll();
}

async function loadModelSettings() {
  try {
    const config = await api("/api/llm/config");
    $("#llmApiUrlInput").value = config.api_url || "https://ai-service.segway-ninebot.com";
    const model = config.model || "";
    const select = $("#llmModelSelect");
    const hasOption = [...select.options].some((option) => option.value === model);
    select.value = hasOption ? model : "";
    $("#llmModelInput").value = hasOption ? "" : model;
    $("#settingsBtn").textContent = config.configured ? `模型设置 · ${config.model || "已配置"}` : "模型设置";
  } catch (_) {}
}

async function readModelList() {
  const apiUrl = $("#llmApiUrlInput").value.trim();
  const apiKey = $("#llmApiKeyInput").value.trim();
  if (!apiKey) throw new Error("请先填写 API Key。");
  const result = await api("/api/llm/models", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({api_url:apiUrl, api_key:apiKey})});
  const select = $("#llmModelSelect");
  select.innerHTML = `<option value="">选择模型</option>` + (result.models || []).map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("");
  toast(`读取到 ${result.models?.length || 0} 个模型。`);
}

async function saveModelSettings() {
  const apiUrl = $("#llmApiUrlInput").value.trim();
  const apiKey = $("#llmApiKeyInput").value.trim();
  const selected = $("#llmModelSelect").value.trim();
  const manual = $("#llmModelInput").value.trim();
  const model = manual || selected;
  if (!apiKey || !model) throw new Error("请填写 API Key，并选择或填写模型名。");
  const result = await api("/api/llm/config", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({api_url:apiUrl, api_key:apiKey, model})});
  $("#llmApiKeyInput").value = "";
  $("#settingsDialog").close();
  $("#settingsBtn").textContent = `模型设置 · ${result.model}`;
  toast(`模型配置已保存：${result.model}（仅当前工作台进程）`);
}

async function loadSummarySkills() {
  try {
    const result = await api("/api/summary-skills");
    state.summarySkills = result.skills || [];
    const select = $("#summarySkillSelect");
    select.innerHTML = state.summarySkills.map((skill) => `<option value="${escapeHtml(skill.id)}">${escapeHtml(skill.title)}</option>`).join("");
    select.value = state.project?.latest_report_skill || result.default || "meeting-minutes-synthesis-zh";
  } catch (error) { toast(error.message, true); }
}

async function generateReport() {
  try {
    const missing = unresolvedSpeakers(state.project);
    if (!state.project?.transcript_segments?.length) throw new Error("请先完成识别，再生成总结报告。");
    if (missing.length) throw new Error(`请先确认以下发言人：${missing.map(speakerLabel).join("、")}；可直接使用默认姓名，也可改成真实姓名`);
    const skill = $("#summarySkillSelect")?.value || "meeting-minutes-synthesis-zh";
    const job = await api(`/api/projects/${state.project.id}/generate-report`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({skill})});
    toast("正在生成总结报告；长逐字稿会自动分段提取后汇总。");
    jobStatus(job, async () => {
      await refreshProject();
      if (state.project?.latest_report_skill) window.open(`/api/projects/${state.project.id}/download/report/${encodeURIComponent(state.project.latest_report_skill)}`);
    });
  } catch (error) { toast(error.message, true); }
}

function toggleResultExportMenu() {
  $("#resultExportMenu").classList.toggle("hidden");
}

async function saveGlossary() {
  try {
    const glossary = $("#glossaryInput").value;
    state.project = await api(`/api/projects/${state.project.id}/glossary`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({glossary})});
    renderProject();
    toast(`业务词表已保存：${state.project.glossary?.length || 0} 个术语。`);
  } catch (error) { toast(error.message, true); }
}

async function createFromPath() {
  const title = $("#titleInput")?.value.trim() || "";
  const youtubeUrl = $("#youtubeUrlInput")?.value.trim() || "";
  const mediaPath = $("#mediaPathInput")?.value.trim() || "";
  const clipStart = $("#youtubeClipStartInput")?.value.trim() || "0";
  const clipDuration = $("#youtubeClipDurationInput")?.value.trim() || "";
  const expectedSpeakers = $("#expectedSpeakersInput")?.value.trim() || "";
  const glossary = $("#projectGlossaryInput")?.value.trim() || "";
  const mediaInput = $("#mediaFileInput");
  const files = mediaInput?.files ? Array.from(mediaInput.files) : [];
  if (!youtubeUrl && !mediaPath && !files.length) throw new Error("请填写 YouTube 链接、本机路径，或选择一个或多个音频/视频文件。")
  let project;
  if (youtubeUrl) {
    project = await api("/api/projects/youtube", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({url: youtubeUrl, title, expected_speakers: expectedSpeakers, glossary, clip_start: clipStart, clip_duration: clipDuration})});
  } else if (files.length) {
    const form = new FormData(); form.append("title", title); form.append("expected_speakers", expectedSpeakers); form.append("glossary", glossary);
    files.forEach((file) => form.append("file", file, file.name));
    project = await api("/api/projects/upload", { method: "POST", body: form });
  } else {
    project = await api("/api/projects", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title, media_path: mediaPath, expected_speakers: expectedSpeakers, glossary, diarization_path: $("#initialDiarInput")?.value.trim() || "", reference_text_path: $("#referenceTextInput")?.value.trim() || ""})});
  }
  state.project = project; renderProject(); $("#projectDialog").close(); toast("本地项目已创建。");
}

function saveSpeakers() {
  const map = structuredClone(state.project.speaker_map || {});
  document.querySelectorAll("#speakerList input").forEach((input) => { map[input.dataset.speaker][input.dataset.field] = input.value.trim(); });
  api(`/api/projects/${state.project.id}/speakers`, {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({speaker_map:map})})
    .then((project) => {state.project=project;renderProject();toast("人物标记已确认。");}).catch((e)=>toast(e.message,true));
}

function collectTranscript() {
  return (state.project.transcript_segments || []).map((item, index) => {
    const row = document.querySelector(`.transcript-row[data-index="${index}"]`);
    return {...item, speaker: row?.querySelector('[data-field="speaker"]')?.value || item.speaker, text: row?.querySelector('[data-field="text"]')?.value.trim() || ""};
  });
}

function saveTranscript() {
  api(`/api/projects/${state.project.id}/transcript`, {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({segments:collectTranscript()})})
    .then((project)=>{state.project=project;renderProject();toast("逐字稿修改已保存。");}).catch((e)=>toast(e.message,true));
}

async function importResult(kind) {
  const input = kind === "diar" ? $("#diarPathInput") : $("#asrPathInput");
  const endpoint = kind === "diar" ? "diarization" : "asr";
  try { const project = await api(`/api/projects/${state.project.id}/import/${endpoint}`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:input.value.trim()})}); state.project=project;renderProject();toast("结果已导入。"); } catch(e){toast(e.message,true)}
}

async function loadDemo() { try { state.project = await api("/api/projects/demo", {method:"POST"}); renderProject(); toast("已打开“新录音 8”示例。") } catch(e){toast(e.message,true)} }

async function openProjectDialog() {
  try {
    const projects = await api("/api/projects");
    const box = $("#existingProjects");
    box.innerHTML = projects.length ? `<div class="existing-title">已有本地项目</div>${projects.map((item) => `<button type="button" class="project-choice" data-project-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.updated_at || "")}</small></button>`).join("")}` : "";
    box.querySelectorAll("[data-project-id]").forEach((button) => button.onclick = async () => { state.project = await api(`/api/projects/${button.dataset.projectId}`); renderProject(); $("#projectDialog").close(); toast("已切换本地项目。"); });
    $("#projectDialog").showModal();
  } catch (error) { toast(error.message, true); }
}
$("#settingsBtn").onclick = async () => { await loadModelSettings(); $("#settingsDialog").showModal(); };
$("#refreshModelsBtn").onclick = () => readModelList().catch((e) => toast(e.message, true));
$("#llmModelSelect").onchange = () => { if ($("#llmModelSelect").value) $("#llmModelInput").value = ""; };
$("#llmModelInput").oninput = () => { if ($("#llmModelInput").value.trim()) $("#llmModelSelect").value = ""; };
$("#saveSettingsBtn").onclick = (event) => { event.preventDefault(); saveModelSettings().catch((e) => toast(e.message, true)); };
$("#generateMinutesBtn").onclick = generateReport;
$("#resultExportToggle").onclick = toggleResultExportMenu;
document.addEventListener("click", (event) => { if (!event.target.closest(".result-export")) $("#resultExportMenu").classList.add("hidden"); });
$("#generateMinutesAdvancedBtn").onclick = generateReport;
$("#saveGlossaryBtn").onclick = saveGlossary;
async function openSpeakerCorrectionDialog() {
  const count = Object.keys(state.project?.speaker_map || {}).length;
  const summary = $("#speakerCorrectionSummary");
  if (summary) summary.textContent = `系统当前识别到 ${count} 位说话人。`;
  const input = $("#actualSpeakerCountInput");
  input.value = state.project?.expected_speakers || count || "";
  $("#speakerCorrectionDialog").showModal();
}
async function correctSpeakerCount() {
  try {
    const value = $("#actualSpeakerCountInput").value.trim();
    if (!value) throw new Error("请填写实际说话人数。");
    $("#confirmSpeakerCorrectionBtn").disabled = true;
    const job = await api(`/api/projects/${state.project.id}/process/correct-speakers`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({expected_speakers:value})});
    $("#speakerCorrectionDialog").close();
    toast(`已按 ${value} 位说话人重新分离，请稍候。`);
    jobStatus(job);
  } catch (error) { toast(error.message, true); }
  finally { $("#confirmSpeakerCorrectionBtn").disabled = false; }
}
$("#openSpeakerCorrectionBtn").onclick = openSpeakerCorrectionDialog;
$("#confirmSpeakerCorrectionBtn").onclick = (event) => { event.preventDefault(); correctSpeakerCount().catch((error)=>toast(error.message,true)); };
$("#newProjectBtn").onclick = openProjectDialog;
$("#emptyImportBtn").onclick = openProjectDialog;
$("#loadDemoBtn").onclick = loadDemo; $("#emptyDemoBtn").onclick = loadDemo;
$("#createProjectBtn").onclick = (event) => { event.preventDefault(); createFromPath().catch((e)=>toast(e.message,true)); };
$("#switchProjectBtn").onclick = openProjectDialog;
$("#saveSpeakersBtn").onclick = saveSpeakers;
$("#importDiarBtn").onclick = () => importResult("diar");
$("#importAsrBtn").onclick = () => importResult("asr");
$("#runDiarBtn").onclick = () => $("#tokenDialog").showModal();
$("#confirmDiarBtn").onclick = async (event) => { event.preventDefault(); const token=$("#hfTokenInput").value.trim(); const offline=$("#offlineModeInput").checked; try{const job=await api(`/api/projects/${state.project.id}/process/diarize`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token, offline})});$("#hfTokenInput").value="";$("#offlineModeInput").checked=false;$("#tokenDialog").close();jobStatus(job)}catch(e){toast(e.message,true)}};
$("#runAsrBtn").onclick = async () => { try{const language=$("#asrLanguage").value; const job=await api(`/api/projects/${state.project.id}/process/qwen-speaker-aware`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({language})});jobStatus(job)}catch(e){toast(e.message,true)} };
$("#runPipelineBtn").onclick = async () => { try{const language=$("#asrLanguage").value; const job=await api(`/api/projects/${state.project.id}/process/full-pipeline`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({language})});toast("已开始完整识别：本地 Speaker 分离 → 公司 qwen3-asr 署名转写。 ");jobStatus(job)}catch(e){toast(e.message,true)} };
$("#alignBtn").onclick = async () => { try{state.project=await api(`/api/projects/${state.project.id}/align`,{method:"POST"});renderProject();toast("已生成带 Speaker 的逐字稿草稿。") }catch(e){toast(e.message,true)} };
$("#saveTranscriptBtn").onclick = saveTranscript;
async function exportTranscript() {
  try {
    const missing = unresolvedSpeakers(state.project);
    if (missing.length) throw new Error(`请先为以下发言人填写姓名：${missing.map(speakerLabel).join("、")}；角色可留空`);
    await saveTranscript();
    const links = await api(`/api/projects/${state.project.id}/export`, {method:"POST"});
    toast("已生成带时间戳和人名的逐字稿：Markdown、TXT、SRT。");
    Object.values(links).forEach((url, i) => setTimeout(() => window.open(url, "_blank"), i * 250));
  } catch (e) { toast(e.message, true); }
}
$("#exportBtn").onclick = exportTranscript;
$("#simpleExportBtn").onclick = exportTranscript;

loadModelSettings();
loadSummarySkills();
api("/api/projects").then((projects)=>{ if(projects.length){ state.project={id:projects[0].id}; refreshProject(); } }).catch(()=>{});
