# 多听工作台

本项目将音频/视频转成带 Speaker、姓名、角色、时间戳的逐字稿，并可调用公司兼容 OpenAI API 的大模型生成会议总结纪要。

## 核心流程

```text
音频 / 视频 / YouTube
  → 本地 pyannote Speaker 分离
  → 公司 qwen3-asr Speaker-aware 转写
  → 人工确认姓名 / 角色
  → 导出带时间戳逐字稿
  → 生成会议总结纪要
```

## 本地启动

使用 Python 3.11+，并安装 ffmpeg、pyannote 运行环境和依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

启动前配置本机依赖路径：

```powershell
$env:FFMPEG_PATH = "C:\\path\\to\\ffmpeg.exe"
$env:VOICEID_PYTHON = "C:\\path\\to\\python.exe"
$env:ASR_API_KEY = "<your-qwen-api-key>"
$env:ASR_API_URL = "https://ai-service.segway-ninebot.com/v1/audio/transcriptions"
$env:MINUTES_SKILL_PATH = "$PWD\\skills\\meeting-minutes-synthesis-zh\\SKILL.md"
python app.py
```

打开：`http://127.0.0.1:8765/`

## 模型设置

会议纪要模型在页面“模型设置”中配置：

- API URL 默认 `https://ai-service.segway-ninebot.com`；
- API Key 只保存在当前进程内存，不提交到 Git；
- 模型可通过 `/v1/models` 获取，也可手动填写；
- 纪要 Skill 位于 `skills/meeting-minutes-synthesis-zh/SKILL.md`。

## 多文件会议

可以一次选择多个音频/视频。系统按选择顺序拼接，并保留每个源文件在总时间轴中的范围，然后对合并音频做全局 Speaker 分离。

## Git 安全

本仓库只提交源代码、配置示例、Skill 和文档，不提交：

- 音频、视频和 YouTube 下载文件；
- `data/projects` 运行时项目数据；
- 模型缓存和 Python 虚拟环境；
- API Key、HF token、日志和临时脚本。
