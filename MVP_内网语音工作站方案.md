# 内网语音工作站与逐字稿 MVP 方案

> 创建日期：2026-09-08  
> 位置：`C:\Users\yang.cheng01\Documents\语音&视频制作\speaker_transcript_studio`

## 1. 目标

先验证一条可以从原始音视频走到可交付成果的本地闭环：

```text
导入音频 / 视频
  → 16 kHz 单声道 WAV
  → 本地说话人分离（pyannote）
  → 带时间戳 ASR
  → 时间重叠对齐
  → 人工标记姓名与角色
  → 编辑校对
  → 导出 Markdown / TXT / SRT
```

MVP 的成功标准不是跨录音自动识别人的真实身份，而是使用者能把 `SPEAKER_XX` 人工命名并得到可核对、可总结的带署名逐字稿。

## 2. 当前状态

### 已完成

- `pyannote/speaker-diarization-3.1` 已本地缓存并验证可在 `HF_HUB_OFFLINE=1` 下运行。
- 完整录音“新录音 8”已完成说话人分离。
- 本地 Web 工作台已实现：导入、speaker 名称/角色标记、时间线、ASR JSON 导入、自动对齐、MD/TXT/SRT 导出。
- 当前模型缓存约 31 MB；完整 Python / PyTorch / pyannote 运行环境约 1.7 GB。

### 已知阻塞

- 路线 A（公司 qwen3-asr）在 2026-09-08 当前网络下出现 TLS EOF，无法验证 `verbose_json` / segment timestamps。
- 现有 `audio8_transcript.txt` 是纯文本，不含时间戳，不能可靠自动匹配 Speaker。

## 3. MVP 范围与决策

### MVP 边界

- 单机、本地浏览器、单用户；不开放内网给同事。
- 用 pyannote 做匿名 speaker diarization。
- 本地 ASR 必须输出 segment 级 `start/end/text`。
- 使用者在 UI 中手动将 `SPEAKER_XX` 改为姓名和角色。
- 输出带时间戳、带人名的 Markdown / TXT / SRT。

### ASR 决策

1. 保留“路线 A”作为公司 ASR 的高质量入口；网络恢复后再验证 verbose JSON。
2. 新增本地 timestamp ASR 作为 MVP 兜底：优先使用 `faster-whisper` 的公开多语种模型。
3. MVP 首先处理 1–3 分钟片段验证完整流程；成功后再处理长录音。

## 4. MVP 流程

```text
[浏览器导入媒体]
       ↓
[服务端提取 WAV]
       ↓
[pyannote 离线 speaker diarization]
       ↓
[faster-whisper 本地 ASR + segment timestamps]
       ↓
[按时间重叠匹配 ASR 段与 SPEAKER]
       ↓
[用户修改 SPEAKER 名称 / 角色 / 文本]
       ↓
[导出 Markdown / TXT / SRT]
```

## 5. 验收标准

一次 MVP 测试满足以下条件即通过：

- [ ] 可导入一条原始 `.m4a` / `.mp4` / `.wav` 文件；
- [ ] 生成至少 1 个 `SPEAKER_XX` 时间段；
- [ ] 生成至少 1 条含 `start/end/text` 的 ASR segment；
- [ ] 自动生成署名逐字稿草稿；
- [ ] 在 UI 中将一个 speaker 改为姓名/角色并保存；
- [ ] 成功导出 `.md`、`.txt`、`.srt`；
- [ ] 导出的内容可读、时间戳递增、人物标签正确显示。

## 6. MVP 后的内网版规划（暂不实施）

### 推荐架构

```text
同事浏览器 → 内网工作站 API → 任务队列 → 本地模型离线推理 → 结果存储
```

### 必做安全改造

- 移除远程用户填写服务端任意绝对路径的能力；
- 改为上传或受控共享盘目录；
- 增加公司 SSO / 内部账号或至少 IP 白名单；
- 项目与文件按用户/部门隔离；
- 增加队列、并发限制、文件保留期限和审计日志；
- 离线运行时不向用户暴露 HF token。

## 7. 真实声纹身份识别（后续）

当前 MVP 只做 speaker diarization + 人工命名。若需要跨录音自动识别“这是谁”，后续增加：

```text
注册声样（每人 30–60 秒干净语音）
  → speaker embedding
  → 与 diarization cluster 比对
  → 阈值 / UNKNOWN 策略
```

此能力涉及生物特征数据，应额外确认员工/受访者告知、授权、数据保留和访问权限。

## 8. 2026-09-08 MVP 实测结果

### 样本

- 项目：`MVP — 120秒中文对话（带署名逐字稿）`
- 输入：`会话总结_0826\seg_test_120s.wav`
- 处理时长：120 秒

### 结果

- 离线 pyannote 说话人分离：2 位 speaker，50 个时间片段；
- 本地 faster-whisper small 中文 ASR：72 个 `start/end/text` 片段；
- 对齐：72 个逐字稿片段均获得 speaker 标签；
- 人工角色标记：`说话人 A（主要讲述者）`、`说话人 B（对话对象）`；
- 成功导出 Markdown、TXT、SRT。

### 导出目录

```text
C:\Users\yang.cheng01\Documents\语音&视频制作\speaker_transcript_studio\data\projects\659c03cf6d\exports
```

### 质量结论

端到端技术流程已验证可行。`faster-whisper small` 的中文文本存在口音、专有名词和口语错误，因此 MVP 输出应进入人工校对环节；之后若路线 A 的 qwen3-asr 可返回 timestamps，建议用其替换本地 ASR 文本，并继续复用相同的 speaker 对齐和编辑导出流程。

### 多语言补充验证

“韩国拜访”前三分钟包含韩语内容。选择韩语后，本地 ASR 生成了 8 个带时间戳韩语片段；工具对韩语会关闭 VAD，避免过滤器过度裁剪有效语音。

## 9. 播放与 Speaker 审核功能（2026-09-08）

浏览器无法播放音频的实际原因是本地 Flask 服务停止，浏览器无法再访问 `/api/projects/<id>/media`；不是 WAV 格式不受支持。服务恢复后，媒体接口返回 `audio/wav` 且支持 Range 请求，浏览器播放已恢复。

Speaker 卡片已增加代表文本、累计时长、代表片段时间与短 WAV 试听按钮，便于使用者在填写姓名/角色前进行确认。

## 10. qwen3-asr 接入结论（2026-09-08）

当前公司 qwen3-asr 服务已恢复可访问。实测对 `response_format=verbose_json` 和 `timestamp_granularities[]=segment` 的请求仍只返回 text，不返回标准 `segments[start,end,text]`。

因此正式推荐链路调整为：

```text
本地 pyannote Speaker 分离（先）
  → 合并同一 Speaker 的短时间段为不超过约 18 秒的转写块
  → 每块调用 qwen3-asr（文字）
  → 转写块继承本地 Speaker、start/end
  → 自动形成可编辑带署名逐字稿
```

这满足“pyannote 本地、qwen3-asr 公司转写”的要求。qwen3-asr 长音频整段转写可与 pyannote 并行，但由于其当前无 native timestamps，只适合作为参考文字，不能可靠自动绑定 Speaker。

qwen 返回的 `language Chinese<asr_text>` 等传输标签已在进入项目数据前自动清理。

### qwen3-asr Speaker-aware 实测（120 秒中文对话）

- pyannote：50 个 Speaker 时间片段；
- 同一 Speaker 的相邻片段合并为 36 个、不超过约 18 秒的 qwen 转写块；
- qwen3-asr：36 个文本块；
- 自动归属：36 段带 Speaker 逐字稿；
- qwen 文本整体优于本地 `faster-whisper small`，且无须 qwen native timestamps；
- 120 秒音频的 qwen Speaker-aware 转写约耗时 2 分 40 秒，分片顺序调用。

## 11. 交付界面收敛（2026-09-08）

默认产品命名为“多听工作台”，并收敛为：

```text
导入音视频 → 开始识别 → 确认人名 → 导出逐字稿
```

默认页面仅展示三步：自动识别、确认人名、导出逐字稿。单独运行模型、导入 JSON、重新对齐、技术状态等仅在“高级操作”下显示。

导出前系统会检查参与逐字稿的 Speaker 是否仍是 `SPEAKER_XX`；未填写姓名/角色时拒绝导出并提示需要命名的 Speaker。

“开始识别”执行本地 pyannote → qwen3-asr Speaker-aware 转写 → 自动生成署名逐字稿。


## 12. 两人对话测试案例修正（2026-09-09）

初次对 `DT-POC-2026-001` 运行 pyannote 时未指定说话人数，模型将极短插话、重叠或噪声拆成了 5 个匿名 Speaker。由于已知该案例只有两个人对话，已重新使用 `num_speakers=2` 运行。

当前测试案例已修正为：

- 2 位 Speaker；
- 12 个参考文字片段；
- 每位 Speaker 卡片显示代表文字；
- 每位 Speaker 可试听代表片段；
- 导出前不会再要求标记不存在的 `SPEAKER_02/03/04`。

导入新项目时，若已知对话人数，可在“已知说话人数”中填写，例如两人对话填写 `2`，避免无约束 diarization 产生额外匿名标签。


## 13. 多文件会议项目（2026-09-10）

工作台现在支持一次导入多个音频/视频文件。文件按选择顺序进入同一个项目，服务端会：

1. 保存每个源文件；
2. 分别转为 16 kHz 单声道 WAV；
3. 拼接为一条连续会议时间轴；
4. 记录每个文件在总时间轴中的 `start/end`；
5. 对拼接后的整场音频运行 pyannote，避免同一人跨文件被重复命名；
6. 继续走 qwen3-asr Speaker-aware 转写；
7. 最终输出一份完整会议逐字稿。

已通过回归验证：2 个 WAV 文件合并为 75 秒连续 WAV，项目 `source_count=2`，manifest 正确记录两个源文件的 0–15 秒和 15–75 秒区间。
