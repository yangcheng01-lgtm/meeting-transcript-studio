"""本地说话人逐字稿工作台。

功能：
- 导入音频/视频或使用已有 diarization/ASR 结果；
- 调用 pyannote 进行说话人分离；
- 尝试通过 qwen3-asr 的 verbose_json 路线取得时间戳文本；
- 人工给匿名 SPEAKER 标签命名为姓名/角色；
- 将时间戳 ASR 与 speaker timeline 对齐并导出 Markdown/TXT/SRT。

所有项目文件都保存在本机配置的数据目录；浏览器服务只绑定 127.0.0.1。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("MEETING_WORKSPACE", APP_DIR.parent))
DATA_ROOT = Path(os.environ.get("MEETING_DATA_ROOT", str(APP_DIR))).expanduser()
PROJECTS_DIR = Path(
    os.environ.get("MEETING_PROJECTS_DIR", str(DATA_ROOT / "data" / "projects"))
).expanduser()
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
FFMPEG = Path(os.environ.get("FFMPEG_PATH", r"D:\Program Files\JianyingPro\11.0.0.14274\ffmpeg.exe"))
VOICEID_PYTHON = Path(os.environ.get("VOICEID_PYTHON", sys.executable))
DIARIZE_SCRIPT = Path(os.environ.get("DIARIZE_SCRIPT", str(APP_DIR / "scripts" / "diarize.py")))
ASR_CONFIG = Path(os.environ.get("ASR_CONFIG_PATH", str(APP_DIR / "config" / "asr.json")))
MINUTES_SKILL = Path(os.environ.get("MINUTES_SKILL_PATH", str(APP_DIR / "skills" / "meeting-minutes-synthesis-zh" / "SKILL.md")))
MAX_CHUNK_SECONDS = 300
LOCAL_ASR_MODEL = os.environ.get("LOCAL_ASR_MODEL", "small")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB local upload cap
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.RLock()
# LLM configuration is intentionally process-memory only. It is never persisted in projects.
_llm_config: dict[str, str] = {"api_url": "https://ai-service.segway-ninebot.com", "api_key": "", "model": ""}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def project_path(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_project(project_id: str) -> dict[str, Any]:
    path = project_path(project_id)
    if not path.is_file():
        raise FileNotFoundError("项目不存在")
    return read_json(path)


def save_project(project: dict[str, Any]) -> None:
    project["updated_at"] = now()
    write_json(project_path(project["id"]), project)


def public_project(project: dict[str, Any]) -> dict[str, Any]:
    """返回给浏览器的项目数据；不会包含凭证。"""
    return project


def fmt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hour = int(seconds // 3600)
    minute = int((seconds % 3600) // 60)
    second = seconds % 60
    return f"{hour:02d}:{minute:02d}:{second:05.2f}"


def fmt_srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hour = int(seconds // 3600)
    minute = int((seconds % 3600) // 60)
    second = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        second += 1
        ms = 0
    return f"{hour:02d}:{minute:02d}:{second:02d},{ms:03d}"


def make_speaker_map(segments: list[dict], existing: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    palette = ["#e05a33", "#1b9aaa", "#d99e28", "#7357d8", "#5f9e5e", "#bc4e84", "#c76f21"]
    mapping = existing or {}
    for index, speaker in enumerate(sorted({str(item.get("speaker", "UNKNOWN")) for item in segments})):
        mapping.setdefault(speaker, {"name": speaker, "role": "", "color": palette[index % len(palette)]})
    return mapping


def clean_qwen_text(text: str) -> str:
    """Remove qwen3-asr transport tags without altering normal spoken content."""
    text = re.sub(r"(?:language\s+\w+)?<asr_text>", "", str(text), flags=re.IGNORECASE)
    text = text.replace("</asr_text>", "")
    return re.sub(r"\s+", " ", text).strip()


def normalize_diarization(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("segments", payload.get("diarization", []))
    if not isinstance(payload, list):
        raise ValueError("说话人分离 JSON 必须包含 segments 数组")
    items = []
    for item in payload:
        try:
            start, end = float(item["start"]), float(item["end"])
            if end <= start:
                continue
            items.append({"start": round(start, 3), "end": round(end, 3), "speaker": str(item["speaker"])})
        except (KeyError, TypeError, ValueError):
            continue
    if not items:
        raise ValueError("未发现有效的 speaker 片段")
    return sorted(items, key=lambda x: (x["start"], x["end"]))


def normalize_asr(payload: Any) -> tuple[list[dict], str]:
    """接收 OpenAI verbose_json 或通用 {segments:[...]}。"""
    if isinstance(payload, list):
        raw_segments = payload
        text = ""
    elif isinstance(payload, dict):
        raw_segments = payload.get("segments") or payload.get("transcript_segments") or payload.get("results") or []
        text = str(payload.get("text", ""))
    else:
        raise ValueError("ASR JSON 格式不正确")
    if not isinstance(raw_segments, list):
        raise ValueError("ASR JSON 中未找到 segments 数组")
    items = []
    for item in raw_segments:
        try:
            start, end = float(item["start"]), float(item["end"])
            segment_text = str(item.get("text", item.get("word", ""))).strip()
            if end <= start or not segment_text:
                continue
            items.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": segment_text,
                "timing_quality": str(item.get("timing_quality", "segment")),
            })
        except (KeyError, TypeError, ValueError):
            continue
    if not items:
        raise ValueError("ASR JSON 中没有可用的带时间戳文字片段")
    return sorted(items, key=lambda x: (x["start"], x["end"])), text


def media_to_wav(project: dict[str, Any]) -> Path:
    """Normalize one or many source files into one continuous 16k mono WAV.

    Multi-file meetings are concatenated in import order before diarization so the
    same person can be clustered consistently across all recordings.
    """
    existing = Path(project.get("audio_path", ""))
    if existing.is_file():
        return existing
    raw_sources = project.get("media_paths") or ([project.get("media_path")] if project.get("media_path") else [])
    sources = [Path(str(item)) for item in raw_sources if item]
    if not sources:
        raise FileNotFoundError("项目没有可用的源文件")
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"源文件不存在：{source}")
    if not FFMPEG.is_file():
        raise FileNotFoundError(f"未找到 ffmpeg：{FFMPEG}")

    output = project_dir(project["id"]) / "audio_16k_mono.wav"
    normalized_dir = project_dir(project["id"]) / "normalized_sources"
    normalized_dir.mkdir(exist_ok=True)
    normalized = []
    manifest = []
    offset = 0.0
    for index, source in enumerate(sources, 1):
        part = normalized_dir / f"part_{index:03d}.wav"
        if not part.is_file():
            cmd = [str(FFMPEG), "-y", "-i", str(source), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(part)]
            process = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if process.returncode != 0:
                raise RuntimeError(process.stderr[-1800:])
        duration = wav_duration(part)
        normalized.append(part)
        manifest.append({
            "index": index,
            "source": str(source),
            "filename": source.name,
            "start": round(offset, 3),
            "end": round(offset + duration, 3),
            "duration": round(duration, 3),
        })
        offset += duration

    if len(normalized) == 1:
        # Keep one-file projects lightweight: use the normalized file directly.
        if normalized[0] != output:
            shutil.copy2(normalized[0], output)
    else:
        concat_list = project_dir(project["id"]) / "normalized_sources.txt"
        lines = []
        for part in normalized:
            safe = str(part.resolve()).replace("\\", "/").replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [str(FFMPEG), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(output)]
        process = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if process.returncode != 0:
            raise RuntimeError(process.stderr[-1800:])

    project["audio_path"] = str(output)
    project["media_paths"] = [str(source) for source in sources]
    project["file_manifest"] = manifest
    project["source_count"] = len(sources)
    save_project(project)
    return output


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def create_job(project_id: str, kind: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "project_id": project_id, "kind": kind, "status": "queued", "message": "等待启动", "created_at": now()}
    return job_id


def update_job(job_id: str, **changes: Any) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(changes)


def job_error(job_id: str, exc: Exception) -> None:
    update_job(job_id, status="error", message=str(exc), finished_at=now())


def run_diarization(project_id: str, job_id: str, token: str | None, offline: bool = False) -> None:
    try:
        update_job(job_id, status="running", message="准备 16 kHz 单声道音频…")
        project = load_project(project_id)
        audio = media_to_wav(project)
        out = project_dir(project_id) / "diarization"
        out.mkdir(exist_ok=True)
        env = os.environ.copy()
        if token:
            env["HF_TOKEN"] = token
        if offline:
            env["HF_HUB_OFFLINE"] = "1"
        env["PYTHONUTF8"] = "1"
        env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        update_job(job_id, message="pyannote 正在做全局说话人分离…")
        command = [str(VOICEID_PYTHON), str(DIARIZE_SCRIPT), str(audio), "-o", str(out)]
        if offline:
            command.append("--offline")
        expected = project.get("expected_speakers")
        if expected:
            command.extend(["--num-speakers", str(int(expected))])
        process = subprocess.run(
            command,
            cwd=str(WORKSPACE), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if process.returncode != 0:
            raise RuntimeError(process.stderr[-1800:] or process.stdout[-1800:] or "说话人分离失败")
        diar_file = out / "diarization.json"
        segments = normalize_diarization(read_json(diar_file))
        project = load_project(project_id)
        project["diarization_segments"] = segments
        project["speaker_map"] = make_speaker_map(segments, project.get("speaker_map"))
        project["diarization_source"] = str(diar_file)
        save_project(project)
        update_job(job_id, status="done", message=f"完成：{len(segments)} 个 speaker 片段", finished_at=now())
    except Exception as exc:
        job_error(job_id, exc)


def asr_request(audio_path: Path, config: dict[str, Any], offset: float, language: str | None) -> tuple[list[dict], str, str]:
    """调用公司 qwen3-asr，优先协商 OpenAI 风格 verbose_json + segment timestamps。

    不同网关对可选字段的兼容性可能不同：先请求时间戳格式；若服务拒绝
    可选字段，回退到最小 model 请求。后者若只返回 text，只标记为 coarse，
    调用方不得据此做自动 Speaker 对齐。
    """
    import requests

    model = config.get("model", "qwen3-asr")
    base = {"model": model}
    if language and language != "auto":
        base["language"] = language

    candidates = [
        {**base, "response_format": "verbose_json", "timestamp_granularities[]": "segment"},
        {**base, "response_format": "verbose_json"},
        base,
    ]
    # 去重：语言可能为空时避免重复的 minimal request。
    unique_candidates = []
    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)

    last_error = None
    for data in unique_candidates:
        try:
            with audio_path.open("rb") as file:
                response = requests.post(
                    config["api_url"],
                    headers={"Authorization": f"Bearer {config['api_key']}"},
                    files={"file": (audio_path.name, file, "audio/wav")},
                    data=data,
                    timeout=300,
                )
            # 可选参数不兼容时尝试下一种；网络/认证等错误必须直接暴露。
            if response.status_code in {400, 404, 422}:
                last_error = f"HTTP {response.status_code}: {response.text[:400]}"
                continue
            response.raise_for_status()
            payload = response.json()
            text = clean_qwen_text(payload.get("text", ""))
            raw_segments = payload.get("segments") or []
            segments = []
            if isinstance(raw_segments, list):
                for item in raw_segments:
                    try:
                        content = clean_qwen_text(item.get("text", ""))
                        start = float(item["start"]) + offset
                        end = float(item["end"]) + offset
                        if content and end > start:
                            segments.append({"start": round(start, 3), "end": round(end, 3), "text": content, "timing_quality": "segment"})
                    except (KeyError, TypeError, ValueError):
                        continue
            if segments:
                return segments, text, "segment"
            # 最小请求成功但没有 timestamps：保留文字供阅读，不允许对齐。
            duration = wav_duration(audio_path)
            if text.strip():
                return [{"start": round(offset, 3), "end": round(offset + duration, 3), "text": text.strip(), "timing_quality": "coarse"}], text, "coarse"
            last_error = "ASR 响应中没有 text 或 segments"
        except requests.RequestException:
            raise
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"qwen3-asr 返回非 JSON 或无效 JSON：{exc}") from exc

    raise RuntimeError(f"qwen3-asr 不接受带时间戳请求，且最小兼容请求失败：{last_error or '未知错误'}")


def run_route_a_asr(project_id: str, job_id: str, language: str | None = "zh") -> None:
    try:
        if ASR_CONFIG.is_file():
            config = read_json(ASR_CONFIG)
        else:
            config = {
                "api_url": os.environ.get("ASR_API_URL", "https://ai-service.segway-ninebot.com/v1/audio/transcriptions"),
                "api_key": os.environ.get("ASR_API_KEY", ""),
                "model": os.environ.get("ASR_MODEL", "qwen3-asr"),
                "language": os.environ.get("ASR_LANGUAGE", "zh"),
            }
        if not config.get("api_key"):
            raise FileNotFoundError("未配置 qwen3-asr：请设置 ASR_CONFIG_PATH 或 ASR_API_KEY。")
        project = load_project(project_id)
        update_job(job_id, status="running", message="准备 qwen3-asr 音频分片…")
        audio = media_to_wav(project)
        total = wav_duration(audio)
        chunks_dir = project_dir(project_id) / "asr_chunks"
        chunks_dir.mkdir(exist_ok=True)
        if not FFMPEG.is_file():
            raise FileNotFoundError(f"未找到 ffmpeg：{FFMPEG}")
        all_segments, raw_texts, qualities = [], [], []
        pos, index = 0.0, 0
        # 同一个 ASR 任务的分片顺序发送：避免服务端配额、并发上限和结果顺序问题。
        while pos < total - 0.05:
            duration = min(MAX_CHUNK_SECONDS, total - pos)
            chunk = chunks_dir / f"chunk_{index:03d}.wav"
            cmd = [str(FFMPEG), "-y", "-ss", str(pos), "-t", str(duration), "-i", str(audio), "-ar", "16000", "-ac", "1", str(chunk)]
            command = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if command.returncode != 0:
                raise RuntimeError(command.stderr[-1200:])
            language_label = language or "自动"
            update_job(job_id, message=f"qwen3-asr 转写中（{language_label}）：第 {index + 1} 段，{pos:.0f}s / {total:.0f}s")
            segments, text, quality = asr_request(chunk, config, pos, language)
            all_segments.extend(segments)
            raw_texts.append(text)
            qualities.append(quality)
            pos += duration
            index += 1
        if not all_segments:
            raise RuntimeError("qwen3-asr 未返回有效文字")
        project = load_project(project_id)
        project["asr_segments"] = all_segments
        project["asr_raw_text"] = "\n".join(part for part in raw_texts if part)
        project["asr_timing_quality"] = "segment" if all(item == "segment" for item in qualities) else "coarse"
        project["asr_source"] = f"company qwen3-asr; requested_language={language or 'auto'}"
        save_project(project)
        write_json(project_dir(project_id) / "qwen3_asr_segments.json", {"segments": all_segments, "text": project["asr_raw_text"], "timing_quality": project["asr_timing_quality"]})
        if project["asr_timing_quality"] == "segment":
            message = f"qwen3-asr 完成：{len(all_segments)} 个带时间戳文字片段"
        else:
            message = "qwen3-asr 仅返回纯文本 / 分片级粗时间，不能可靠自动对齐 Speaker"
        update_job(job_id, status="done", message=message, finished_at=now())
    except Exception as exc:
        job_error(job_id, exc)


def build_speaker_asr_blocks(segments: list[dict], max_duration: float = 18.0, join_gap: float = 0.8) -> list[dict]:
    """Merge adjacent same-speaker turns into qwen-friendly chunks.

    Timestamp and speaker identity are inherited from pyannote. 18s avoids oversized
    context while 0.8s bridges tiny hesitations / diarization micro-fragments.
    """
    blocks: list[dict] = []
    for segment in sorted(segments, key=lambda item: (item["start"], item["end"])):
        start, end, speaker = float(segment["start"]), float(segment["end"]), str(segment["speaker"])
        if end - start < 0.18:
            continue
        if blocks:
            previous = blocks[-1]
            merged_duration = end - previous["start"]
            if speaker == previous["speaker"] and start - previous["end"] <= join_gap and merged_duration <= max_duration:
                previous["end"] = max(previous["end"], end)
                continue
        blocks.append({"start": start, "end": end, "speaker": speaker})
    return blocks


def run_qwen_speaker_aware_asr(project_id: str, job_id: str, language: str | None = "zh") -> None:
    """qwen3-asr text + local pyannote boundaries = speaker-attributed timestamp transcript.

    This is the preferred path when qwen returns plain text but no native timestamps.
    It intentionally runs after diarization, because each qwen request is cut from a
    known speaker block and inherits that block's start/end/speaker.
    """
    try:
        if ASR_CONFIG.is_file():
            config = read_json(ASR_CONFIG)
        else:
            config = {
                "api_url": os.environ.get("ASR_API_URL", "https://ai-service.segway-ninebot.com/v1/audio/transcriptions"),
                "api_key": os.environ.get("ASR_API_KEY", ""),
                "model": os.environ.get("ASR_MODEL", "qwen3-asr"),
                "language": os.environ.get("ASR_LANGUAGE", "zh"),
            }
        if not config.get("api_key"):
            raise FileNotFoundError("未配置 qwen3-asr：请设置 ASR_CONFIG_PATH 或 ASR_API_KEY。")
        project = load_project(project_id)
        diarization = project.get("diarization_segments", [])
        if not diarization:
            raise RuntimeError("请先完成本地 Speaker 分离；qwen3-asr 的署名切片依赖 Speaker 时间段。")
        update_job(job_id, status="running", message="准备按 Speaker 切分的 qwen3-asr 音频片段…")
        audio = media_to_wav(project)
        if not FFMPEG.is_file():
            raise FileNotFoundError(f"未找到 ffmpeg：{FFMPEG}")
        blocks = build_speaker_asr_blocks(diarization)
        if not blocks:
            raise RuntimeError("Speaker 分离结果中没有可转写的有效发言片段")
        chunks_dir = project_dir(project_id) / "qwen_speaker_chunks"
        chunks_dir.mkdir(exist_ok=True)
        transcript, raw_texts = [], []
        for index, block in enumerate(blocks, 1):
            duration = block["end"] - block["start"]
            chunk = chunks_dir / f"speaker_{index:04d}_{block['speaker']}.wav"
            command = subprocess.run(
                [str(FFMPEG), "-y", "-ss", str(block["start"]), "-t", str(duration), "-i", str(audio),
                 "-ar", "16000", "-ac", "1", str(chunk)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if command.returncode != 0:
                raise RuntimeError(command.stderr[-1200:])
            language_label = language or "自动"
            update_job(job_id, message=f"qwen3-asr 署名转写（{language_label}）：第 {index}/{len(blocks)} 段")
            response_segments, text, _ = asr_request(chunk, config, block["start"], language)
            # qwen may return verbose segments or only a single text chunk. The local
            # Speaker block remains the authoritative identity for either response.
            if not text.strip() and response_segments:
                text = " ".join(str(item.get("text", "")) for item in response_segments).strip()
            if text.strip():
                transcript.append({
                    "start": round(block["start"], 3),
                    "end": round(block["end"], 3),
                    "text": text.strip(),
                    "speaker_hint": block["speaker"],
                    "timing_quality": "speaker_block",
                })
                raw_texts.append(text.strip())
        if not transcript:
            raise RuntimeError("qwen3-asr 未返回可用文字")
        project = load_project(project_id)
        project["asr_segments"] = transcript
        project["asr_raw_text"] = "\n".join(raw_texts)
        project["asr_timing_quality"] = "speaker_block"
        project["asr_source"] = f"company qwen3-asr + local pyannote speaker blocks; requested_language={language or 'auto'}"
        save_project(project)
        write_json(project_dir(project_id) / "qwen3_speaker_transcript_segments.json", {"segments": transcript, "text": project["asr_raw_text"], "timing_quality": "speaker_block"})
        update_job(job_id, status="done", message=f"qwen3-asr 署名转写完成：{len(transcript)} 个 Speaker 文字块", finished_at=now())
    except Exception as exc:
        job_error(job_id, exc)


def run_recommended_pipeline(project_id: str, job_id: str, language: str | None = "zh") -> None:
    """Recommended sequential pipeline: offline diarization → qwen speaker-aware ASR."""
    try:
        update_job(job_id, status="running", message="准备音频并开始本地 Speaker 分离…")
        media_to_wav(load_project(project_id))
        diar_job = create_job(project_id, "diarization_offline")
        run_diarization(project_id, diar_job, None, True)
        if _jobs[diar_job].get("status") != "done":
            raise RuntimeError(_jobs[diar_job].get("message", "Speaker 分离失败"))
        update_job(job_id, message="Speaker 分离完成，开始按 Speaker 片段调用 qwen3-asr…")
        qwen_job = create_job(project_id, "asr_qwen3_speaker_aware")
        run_qwen_speaker_aware_asr(project_id, qwen_job, language)
        if _jobs[qwen_job].get("status") != "done":
            raise RuntimeError(_jobs[qwen_job].get("message", "qwen3-asr 署名转写失败"))
        project = load_project(project_id)
        project["transcript_segments"] = align_segments(project["asr_segments"], project["diarization_segments"])
        save_project(project)
        update_job(job_id, status="done", message=f"完整识别完成：{len(project['transcript_segments'])} 段带 Speaker 逐字稿", finished_at=now())
    except Exception as exc:
        job_error(job_id, exc)


def run_local_asr(project_id: str, job_id: str, language: str | None = "zh") -> None:
    """MVP 本地时间戳 ASR：faster-whisper small / CPU int8。"""
    try:
        update_job(job_id, status="running", message=f"加载本地 ASR 模型 {LOCAL_ASR_MODEL}…")
        project = load_project(project_id)
        audio = media_to_wav(project)
        from faster_whisper import WhisperModel

        model = WhisperModel(LOCAL_ASR_MODEL, device="cpu", compute_type="int8")
        language_label = language or "自动"
        update_job(job_id, message=f"本地 ASR 转写中（{language_label}、segment 时间戳）…")
        generated, info = model.transcribe(
            str(audio), language=language, beam_size=3,
            # 韩语样本实测 VAD 可能过度裁剪有效对话，因此关闭；中文保持 VAD 以减少静音/幻觉。
            vad_filter=(language != "ko"),
            condition_on_previous_text=True,
        )
        segments, text_parts = [], []
        for index, segment in enumerate(generated, 1):
            text = segment.text.strip()
            if not text or segment.end <= segment.start:
                continue
            segments.append({
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": text,
                "timing_quality": "segment",
            })
            text_parts.append(text)
            if index % 10 == 0:
                update_job(job_id, message=f"本地 ASR 已完成 {index} 个文字片段…")
        if not segments:
            raise RuntimeError("本地 ASR 未输出有效文字片段")
        project = load_project(project_id)
        project["asr_segments"] = segments
        project["asr_raw_text"] = "\n".join(text_parts)
        project["asr_timing_quality"] = "segment"
        project["asr_source"] = f"local faster-whisper:{LOCAL_ASR_MODEL}; requested_language={language or 'auto'}; detected_language={info.language}"
        save_project(project)
        write_json(project_dir(project_id) / "local_asr_segments.json", {"segments": segments, "text": project["asr_raw_text"], "model": LOCAL_ASR_MODEL})
        update_job(job_id, status="done", message=f"本地 ASR 完成：{len(segments)} 个带时间戳片段", finished_at=now())
    except Exception as exc:
        job_error(job_id, exc)


def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def align_segments(asr_segments: list[dict], diarization_segments: list[dict]) -> list[dict]:
    result = []
    for item in asr_segments:
        score: dict[str, float] = {}
        for diar in diarization_segments:
            amount = overlap(item["start"], item["end"], diar["start"], diar["end"])
            if amount:
                score[diar["speaker"]] = score.get(diar["speaker"], 0.0) + amount
        speaker = str(item.get("speaker_hint")) if item.get("speaker_hint") else (max(score, key=score.get) if score else "UNKNOWN")
        result.append({
            "id": uuid.uuid4().hex[:10],
            "start": item["start"],
            "end": item["end"],
            "speaker": speaker,
            "text": item["text"],
            "timing_quality": item.get("timing_quality", "segment"),
        })
    return result


def speaker_preview_window(project: dict[str, Any], speaker: str) -> tuple[float, float]:
    """Choose the longest identified turn as a compact representative listening sample."""
    turns = [item for item in project.get("diarization_segments", []) if item.get("speaker") == speaker]
    if not turns:
        raise ValueError("未找到该 Speaker 的音频片段")
    longest = max(turns, key=lambda item: float(item["end"]) - float(item["start"]))
    start = max(0.0, float(longest["start"]))
    duration = min(10.0, max(1.5, float(longest["end"]) - start))
    return start, duration


def speaker_excerpt(project: dict[str, Any], speaker: str) -> str:
    """Return a short transcript excerpt to help the reviewer identify a speaker."""
    source = project.get("transcript_segments") or []
    lines = [str(item.get("text", "")).strip() for item in source if item.get("speaker") == speaker and str(item.get("text", "")).strip()]
    if not lines and project.get("asr_segments"):
        # Before explicit alignment, assign ASR segment to the speaker with the greatest overlap.
        for item in project["asr_segments"]:
            scores = {}
            for diar in project.get("diarization_segments", []):
                amount = overlap(item["start"], item["end"], diar["start"], diar["end"])
                if amount:
                    scores[diar["speaker"]] = scores.get(diar["speaker"], 0.0) + amount
            if scores and max(scores, key=scores.get) == speaker:
                text = str(item.get("text", "")).strip()
                if text:
                    lines.append(text)
    excerpt = " ".join(lines[:2])
    return excerpt[:180] + ("…" if len(excerpt) > 180 else "")


def speaker_label(project: dict[str, Any], speaker: str) -> str:
    metadata = project.get("speaker_map", {}).get(speaker, {})
    name = str(metadata.get("name") or speaker)
    role = str(metadata.get("role") or "").strip()
    return f"{name}（{role}）" if role else name


def normalize_llm_base_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    if not value:
        return "https://ai-service.segway-ninebot.com"
    return value[:-3] if value.endswith("/v1") else value


def llm_endpoint(path: str) -> str:
    return f"{normalize_llm_base_url(_llm_config.get('api_url'))}/v1/{path.lstrip('/')}"


def llm_headers() -> dict[str, str]:
    key = _llm_config.get("api_key", "").strip()
    if not key:
        raise RuntimeError("请先打开“模型设置”，填写 API Key。")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def transcript_for_agent(project: dict[str, Any]) -> str:
    segments = project.get("transcript_segments") or []
    if not segments:
        raise ValueError("当前项目还没有带 Speaker 的逐字稿，请先完成识别。")
    lines = []
    for item in segments:
        speaker = speaker_label(project, str(item.get("speaker", "UNKNOWN")))
        lines.append(f"[{fmt_timestamp(float(item['start']))} – {fmt_timestamp(float(item['end']))}] {speaker}：{str(item.get('text', '')).strip()}")
    return "\n".join(lines)


def llm_minutes_prompt(project: dict[str, Any]) -> tuple[str, str]:
    if not MINUTES_SKILL.is_file():
        raise FileNotFoundError(f"未找到会议纪要 Skill：{MINUTES_SKILL}")
    skill = MINUTES_SKILL.read_text(encoding="utf-8")
    transcript = transcript_for_agent(project)
    system = f"""你是多听工作台的会议总结纪要 Agent。请严格遵守下面的 Skill 规则。\n\n{skill}\n\n只输出最终中文 Markdown 会议纪要，不要解释你使用了哪些规则，不要输出 JSON。"""
    user = f"""请根据下面项目的带 Speaker、带时间戳逐字稿，生成正式会议总结纪要。\n\n项目标题：{project.get('title', '未命名项目')}\n项目编号：{project.get('project_code', project.get('id', '未提供'))}\n来源文件：{json.dumps(project.get('media_paths') or [project.get('media_path')], ensure_ascii=False)}\n\n逐字稿：\n{transcript}\n\n要求：\n1. 保留关键数字、单位和不确定性口径。\n2. 严格区分事实、决策、正式行动项、建议跟进、风险、开放问题。\n3. 未明确负责人或截止时间时写“未明确”，禁止臆造。\n4. 对可能的 ASR 错词、数字或 Speaker 归属添加复核提示。\n5. 结论先行，适合后续形成会议总结和纪要。"""
    return system, user


def extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("模型响应中没有 choices。")
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content)
    content = str(content).strip()
    if not content:
        raise RuntimeError("模型响应中没有可用的纪要文本。")
    return content


def render_exports(project: dict[str, Any]) -> dict[str, Path]:
    segments = project.get("transcript_segments", [])
    if not segments:
        raise ValueError("没有可导出的已对齐逐字稿。请先点击“开始识别”。")
    unresolved = []
    for speaker in sorted({str(item.get("speaker", "UNKNOWN")) for item in segments if str(item.get("speaker", "UNKNOWN")) != "UNKNOWN"}):
        name = str(project.get("speaker_map", {}).get(speaker, {}).get("name", "")).strip()
        if not name or name == speaker or re.fullmatch(r"SPEAKER[_\s-]*\d+", name, flags=re.IGNORECASE):
            unresolved.append(speaker)
    if unresolved:
        raise ValueError(f"请先在“确认人名”中填写：{'、'.join(unresolved)}。")
    export_dir = project_dir(project["id"]) / "exports"
    export_dir.mkdir(exist_ok=True)
    slug = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", project.get("title", "逐字稿"))[:60] or "逐字稿"
    md_path, txt_path, srt_path = export_dir / f"{slug}_带署名逐字稿.md", export_dir / f"{slug}_带署名逐字稿.txt", export_dir / f"{slug}_带署名逐字稿.srt"
    md = [f"# {project.get('title', '逐字稿')} — 带署名逐字稿", "", f"- 生成时间：{now()}", "- 注：SPEAKER 标签由本地 pyannote 说话人分离生成；姓名/角色由人工确认。", ""]
    txt, srt = [], []
    for index, item in enumerate(segments, 1):
        label = speaker_label(project, item["speaker"])
        line = f"[{fmt_timestamp(item['start'])} – {fmt_timestamp(item['end'])}] {label}：{item['text']}"
        md.append(line)
        txt.append(line)
        srt.extend([str(index), f"{fmt_srt_timestamp(item['start'])} --> {fmt_srt_timestamp(item['end'])}", f"{label}：{item['text']}", ""])
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    txt_path.write_text("\n".join(txt) + "\n", encoding="utf-8")
    srt_path.write_text("\n".join(srt), encoding="utf-8")
    return {"markdown": md_path, "text": txt_path, "srt": srt_path}


@app.get("/api/llm/config")
def get_llm_config():
    return jsonify({"api_url": normalize_llm_base_url(_llm_config.get("api_url")), "model": _llm_config.get("model", ""), "configured": bool(_llm_config.get("api_key"))})


@app.put("/api/llm/config")
def set_llm_config():
    payload = request.get_json(force=True)
    api_url = normalize_llm_base_url(payload.get("api_url") or "https://ai-service.segway-ninebot.com")
    api_key = str(payload.get("api_key") or "").strip()
    model = str(payload.get("model") or "").strip()
    if not api_key:
        return jsonify(error="API Key 不能为空。"), 400
    if not model:
        return jsonify(error="请填写或选择模型名称。"), 400
    _llm_config.update({"api_url": api_url, "api_key": api_key, "model": model})
    return jsonify({"api_url": api_url, "model": model, "configured": True})


@app.post("/api/llm/models")
def list_llm_models():
    import requests
    payload = request.get_json(silent=True) or {}
    api_url = normalize_llm_base_url(payload.get("api_url") or _llm_config.get("api_url"))
    api_key = str(payload.get("api_key") or _llm_config.get("api_key") or "").strip()
    if not api_key:
        return jsonify(error="请先填写 API Key。"), 400
    try:
        response = requests.get(f"{api_url}/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        response.raise_for_status()
        data = response.json()
        models = [str(item.get("id")) for item in (data.get("data") or []) if isinstance(item, dict) and item.get("id")]
        return jsonify({"models": models})
    except Exception as exc:
        return jsonify(error=f"读取模型列表失败：{exc}"), 400


@app.post("/api/projects/<project_id>/generate-minutes")
def generate_minutes(project_id: str):
    try:
        project = load_project(project_id)
        system, user = llm_minutes_prompt(project)
        import requests
        body = {
            "model": _llm_config.get("model"),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
        }
        response = requests.post(llm_endpoint("chat/completions"), headers=llm_headers(), json=body, timeout=600)
        response.raise_for_status()
        content = extract_chat_content(response.json())
        exports = project_dir(project_id) / "exports"
        exports.mkdir(exist_ok=True)
        code = project.get("project_code") or project.get("id")
        minutes_path = exports / f"{code}__OUT__会议总结纪要.md"
        minutes_path.write_text(content + ("\n" if not content.endswith("\n") else ""), encoding="utf-8")
        project["minutes_path"] = str(minutes_path)
        project["minutes_model"] = _llm_config.get("model")
        project["minutes_generated_at"] = now()
        save_project(project)
        return jsonify({"path": f"/api/projects/{project_id}/download/minutes", "model": _llm_config.get("model"), "project": public_project(project)})
    except Exception as exc:
        return jsonify(error=f"生成会议纪要失败：{exc}"), 400


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/projects")
def list_projects():
    projects = []
    for path in PROJECTS_DIR.glob("*/project.json"):
        try:
            project = read_json(path)
            if project.get("hidden"):
                continue
            projects.append({key: project.get(key) for key in ("id", "title", "created_at", "updated_at")})
        except Exception:
            continue
    return jsonify(sorted(projects, key=lambda item: item.get("updated_at", ""), reverse=True))


@app.post("/api/projects")
def create_project():
    payload = request.get_json(force=True)
    media_path = Path(str(payload.get("media_path", "")).strip().strip('"'))
    if not media_path.is_file():
        return jsonify(error="找不到本机媒体文件；请填写完整路径或改用文件选择导入。"), 400
    title = str(payload.get("title", "")).strip() or media_path.stem
    project_id = uuid.uuid4().hex[:10]
    project = {
        "id": project_id,
        "title": title,
        "created_at": now(),
        "updated_at": now(),
        "media_path": str(media_path.resolve()),
        "media_paths": [str(media_path.resolve())],
        "source_count": 1,
        "file_manifest": [],
        "audio_path": "",
        "diarization_segments": [],
        "asr_segments": [],
        "asr_raw_text": "",
        "asr_timing_quality": "",
        "transcript_segments": [],
        "speaker_map": {},
        "expected_speakers": int(payload["expected_speakers"]) if str(payload.get("expected_speakers", "")).isdigit() else None,
    }
    project_dir(project_id).mkdir(parents=True, exist_ok=True)
    reference_path = str(payload.get("reference_text_path", "")).strip().strip('"')
    if reference_path and Path(reference_path).is_file():
        project["reference_text"] = Path(reference_path).read_text(encoding="utf-8", errors="replace")
    diar_path = str(payload.get("diarization_path", "")).strip().strip('"')
    if diar_path and Path(diar_path).is_file():
        segments = normalize_diarization(read_json(Path(diar_path)))
        project["diarization_segments"] = segments
        project["speaker_map"] = make_speaker_map(segments)
        project["diarization_source"] = str(Path(diar_path).resolve())
    save_project(project)
    return jsonify(public_project(project)), 201


@app.post("/api/projects/upload")
def upload_project():
    files = [file for file in request.files.getlist("file") if file and file.filename]
    if not files:
        return jsonify(error="请选择一个或多个音频/视频文件。"), 400
    title = str(request.form.get("title", "")).strip() or (Path(files[0].filename).stem if len(files) == 1 else "多文件会议")
    project_id = uuid.uuid4().hex[:10]
    folder = project_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    sources = []
    for index, file in enumerate(files, 1):
        safe_name = secure_filename(file.filename) or f"source_{index:03d}.media"
        source = folder / f"source_{index:03d}_{safe_name}"
        file.save(source)
        sources.append(source)
    expected_raw = str(request.form.get("expected_speakers", "")).strip()
    expected_speakers = int(expected_raw) if expected_raw.isdigit() and 1 <= int(expected_raw) <= 20 else None
    project = {
        "id": project_id, "title": title, "created_at": now(), "updated_at": now(),
        "media_path": str(sources[0]), "media_paths": [str(source) for source in sources],
        "source_count": len(sources), "audio_path": "", "file_manifest": [],
        "diarization_segments": [], "asr_segments": [], "asr_raw_text": "",
        "asr_timing_quality": "", "transcript_segments": [], "speaker_map": {},
        "expected_speakers": expected_speakers,
    }
    save_project(project)
    return jsonify(public_project(project)), 201


@app.post("/api/projects/youtube")
def create_youtube_project():
    """Download public YouTube audio into a local project; no cookies or login bypass."""
    payload = request.get_json(force=True)
    url = str(payload.get("url", "")).strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    allowed_hosts = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
    if parsed.scheme not in {"http", "https"} or host not in allowed_hosts:
        return jsonify(error="请输入公开 YouTube 链接（youtube.com 或 youtu.be）。"), 400
    expected_raw = str(payload.get("expected_speakers", "")).strip()
    expected_speakers = int(expected_raw) if expected_raw.isdigit() and 1 <= int(expected_raw) <= 20 else None
    project_id = uuid.uuid4().hex[:10]
    folder = project_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        import yt_dlp
        output = folder / "source_%(id)s.%(ext)s"
        options = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": str(output),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            prepared = downloader.prepare_filename(info)
        source = Path(prepared)
        if not source.is_file():
            candidates = sorted(folder.glob("source_*"), key=lambda path: path.stat().st_mtime, reverse=True)
            source = candidates[0] if candidates else source
        if not source.is_file():
            raise RuntimeError("YouTube 音频下载完成但未找到本地文件")
        title = str(payload.get("title", "")).strip() or str(info.get("title") or source.stem)
        project = {
            "id": project_id,
            "title": title,
            "created_at": now(),
            "updated_at": now(),
            "media_path": str(source),
            "audio_path": "",
            "source_url": url,
            "source_type": "youtube_audio",
            "youtube_id": str(info.get("id") or ""),
            "youtube_title": str(info.get("title") or title),
            "youtube_uploader": str(info.get("uploader") or ""),
            "diarization_segments": [],
            "asr_segments": [],
            "asr_raw_text": "",
            "asr_timing_quality": "",
            "transcript_segments": [],
            "speaker_map": {},
            "expected_speakers": expected_speakers,
        }
        save_project(project)
        return jsonify(public_project(project)), 201
    except Exception as exc:
        # Keep a failed project out of the normal list but retain the diagnostic file.
        (folder / "youtube_import_error.txt").write_text(str(exc), encoding="utf-8")
        return jsonify(error=f"YouTube 导入失败：{exc}"), 400


@app.post("/api/projects/demo")
def load_demo():
    base = WORKSPACE / "会话总结_0826"
    media = base / "新录音 8_audio.wav"
    diar = base / "diar_audio8" / "diarization.json"
    text = base / "audio8_transcript.txt"
    if not media.is_file() or not diar.is_file():
        return jsonify(error="未找到当前会话示例文件。"), 404
    project_id = uuid.uuid4().hex[:10]
    segments = normalize_diarization(read_json(diar))
    project = {"id": project_id, "title": "新录音 8 — 访谈", "created_at": now(), "updated_at": now(), "media_path": str(media), "audio_path": str(media), "diarization_segments": segments, "diarization_source": str(diar), "asr_segments": [], "asr_raw_text": "", "asr_timing_quality": "", "transcript_segments": [], "speaker_map": make_speaker_map(segments), "reference_text": text.read_text(encoding="utf-8", errors="replace") if text.is_file() else ""}
    project_dir(project_id).mkdir(parents=True, exist_ok=True)
    save_project(project)
    return jsonify(public_project(project)), 201


@app.get("/api/projects/<project_id>")
def get_project(project_id: str):
    try:
        return jsonify(public_project(load_project(project_id)))
    except FileNotFoundError:
        return jsonify(error="项目不存在"), 404


@app.get("/api/projects/<project_id>/media")
def serve_media(project_id: str):
    project = load_project(project_id)
    media = Path(project.get("audio_path") or project["media_path"])
    if not media.is_file():
        return jsonify(error="媒体文件不存在"), 404
    return send_file(media, conditional=True)


@app.get("/api/projects/<project_id>/speaker-preview/<speaker>")
def speaker_preview(project_id: str, speaker: str):
    """Generate (once) and serve a small MP3 clip for reviewer-assisted speaker naming."""
    try:
        project = load_project(project_id)
        if speaker not in project.get("speaker_map", {}):
            return jsonify(error="Speaker 不存在"), 404
        start, duration = speaker_preview_window(project, speaker)
        audio = media_to_wav(project)
        previews = project_dir(project_id) / "speaker_previews"
        previews.mkdir(exist_ok=True)
        clip = previews / f"{secure_filename(speaker)}_{start:.3f}_{duration:.3f}.wav"
        if not clip.is_file():
            if not FFMPEG.is_file():
                raise FileNotFoundError(f"未找到 ffmpeg：{FFMPEG}")
            process = subprocess.run(
                [str(FFMPEG), "-y", "-ss", str(start), "-t", str(duration), "-i", str(audio),
                 "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(clip)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if process.returncode != 0:
                raise RuntimeError(process.stderr[-1200:])
        return send_file(clip, mimetype="audio/wav", conditional=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/projects/<project_id>/import/diarization")
def import_diarization(project_id: str):
    try:
        payload = request.get_json(force=True)
        source = Path(str(payload.get("path", "")).strip().strip('"'))
        segments = normalize_diarization(read_json(source))
        project = load_project(project_id)
        project["diarization_segments"] = segments
        project["speaker_map"] = make_speaker_map(segments, project.get("speaker_map"))
        project["diarization_source"] = str(source.resolve())
        save_project(project)
        return jsonify(public_project(project))
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/projects/<project_id>/import/asr")
def import_asr(project_id: str):
    try:
        payload = request.get_json(force=True)
        source = Path(str(payload.get("path", "")).strip().strip('"'))
        segments, text = normalize_asr(read_json(source))
        project = load_project(project_id)
        project["asr_segments"] = segments
        project["asr_raw_text"] = text
        project["asr_timing_quality"] = "imported"
        project["asr_source"] = str(source.resolve())
        save_project(project)
        return jsonify(public_project(project))
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/projects/<project_id>/process/diarize")
def start_diarization(project_id: str):
    payload = request.get_json(force=True)
    offline = bool(payload.get("offline", False))
    token = str(payload.get("token", "")).strip() or os.environ.get("HF_TOKEN")
    if not token and not offline:
        return jsonify(error="未检测到 HF_TOKEN。请输入 token / 在启动前设置 HF_TOKEN，或在模型缓存完整时选择离线运行。"), 400
    if not VOICEID_PYTHON.is_file() or not DIARIZE_SCRIPT.is_file():
        return jsonify(error="未找到已配置的 pyannote 运行环境或 diarize.py。"), 500
    job_id = create_job(project_id, "diarization")
    threading.Thread(target=run_diarization, args=(project_id, job_id, token, offline), daemon=True).start()
    return jsonify(_jobs[job_id]), 202


@app.post("/api/projects/<project_id>/process/asr-route-a")
def start_route_a_asr(project_id: str):
    payload = request.get_json(silent=True) or {}
    language = str(payload.get("language", "zh")).strip().lower()
    if language not in {"auto", "zh", "ko", "en"}:
        return jsonify(error="qwen3-asr 语言只支持：auto、zh、ko、en。"), 400
    language = None if language == "auto" else language
    job_id = create_job(project_id, "asr_qwen3")
    threading.Thread(target=run_route_a_asr, args=(project_id, job_id, language), daemon=True).start()
    return jsonify(_jobs[job_id]), 202


@app.post("/api/projects/<project_id>/process/full-pipeline")
def start_full_pipeline(project_id: str):
    """Recommended qwen-only transcript route: Speaker first, qwen second."""
    payload = request.get_json(silent=True) or {}
    language = str(payload.get("language", "zh")).strip().lower()
    if language not in {"auto", "zh", "ko", "en"}:
        return jsonify(error="语言只支持：auto、zh、ko、en。"), 400
    language = None if language == "auto" else language
    job_id = create_job(project_id, "full_qwen_speaker_pipeline")
    threading.Thread(target=run_recommended_pipeline, args=(project_id, job_id, language), daemon=True).start()
    return jsonify(_jobs[job_id]), 202


@app.post("/api/projects/<project_id>/process/qwen-speaker-aware")
def start_qwen_speaker_aware(project_id: str):
    payload = request.get_json(silent=True) or {}
    language = str(payload.get("language", "zh")).strip().lower()
    if language not in {"auto", "zh", "ko", "en"}:
        return jsonify(error="语言只支持：auto、zh、ko、en。"), 400
    language = None if language == "auto" else language
    job_id = create_job(project_id, "asr_qwen3_speaker_aware")
    threading.Thread(target=run_qwen_speaker_aware_asr, args=(project_id, job_id, language), daemon=True).start()
    return jsonify(_jobs[job_id]), 202

@app.post("/api/projects/<project_id>/process/asr-local")
def start_local_asr(project_id: str):
    payload = request.get_json(silent=True) or {}
    language = str(payload.get("language", "zh")).strip().lower()
    allowed = {"auto", "zh", "ko", "en"}
    if language not in allowed:
        return jsonify(error="本地 ASR 语言只支持：auto、zh、ko、en。"), 400
    language = None if language == "auto" else language
    job_id = create_job(project_id, "asr_local")
    threading.Thread(target=run_local_asr, args=(project_id, job_id, language), daemon=True).start()
    return jsonify(_jobs[job_id]), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    return jsonify(job) if job else (jsonify(error="任务不存在"), 404)


@app.put("/api/projects/<project_id>/speakers")
def update_speakers(project_id: str):
    try:
        payload = request.get_json(force=True)
        incoming = payload.get("speaker_map", {})
        project = load_project(project_id)
        known = {str(item["speaker"]) for item in project.get("diarization_segments", [])}
        updated = make_speaker_map(project.get("diarization_segments", []), project.get("speaker_map"))
        for speaker, info in incoming.items():
            if speaker in known and isinstance(info, dict):
                updated[speaker] = {"name": str(info.get("name", speaker)).strip() or speaker, "role": str(info.get("role", "")).strip(), "color": str(info.get("color", updated[speaker].get("color", "#e05a33")))}
        project["speaker_map"] = updated
        save_project(project)
        return jsonify(public_project(project))
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/projects/<project_id>/align")
def align_transcript(project_id: str):
    try:
        project = load_project(project_id)
        if not project.get("diarization_segments"):
            raise ValueError("请先导入或运行说话人分离。")
        if not project.get("asr_segments"):
            raise ValueError("请先导入或运行带时间戳的 ASR。")
        if project.get("asr_timing_quality") == "coarse":
            raise ValueError("当前 qwen3-asr 仅返回分片级粗时间戳，不能可靠自动对齐 Speaker。请使用“完整识别”或“qwen 署名转写”，由本地 Speaker 时间段提供时间轴。")
        project["transcript_segments"] = align_segments(project["asr_segments"], project["diarization_segments"])
        save_project(project)
        return jsonify(public_project(project))
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.put("/api/projects/<project_id>/transcript")
def save_transcript(project_id: str):
    try:
        payload = request.get_json(force=True)
        items = payload.get("segments", [])
        if not isinstance(items, list):
            raise ValueError("segments 必须为数组")
        clean = []
        allowed_speakers = set(load_project(project_id).get("speaker_map", {}).keys()) | {"UNKNOWN"}
        for item in items:
            start, end = float(item["start"]), float(item["end"])
            if end <= start:
                continue
            speaker = str(item.get("speaker", "UNKNOWN"))
            clean.append({"id": str(item.get("id", uuid.uuid4().hex[:10])), "start": round(start, 3), "end": round(end, 3), "speaker": speaker if speaker in allowed_speakers else "UNKNOWN", "text": str(item.get("text", "")).strip(), "timing_quality": str(item.get("timing_quality", "segment"))})
        project = load_project(project_id)
        project["transcript_segments"] = sorted(clean, key=lambda x: (x["start"], x["end"]))
        save_project(project)
        return jsonify(public_project(project))
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/projects/<project_id>/export")
def export_transcript(project_id: str):
    try:
        paths = render_exports(load_project(project_id))
        return jsonify({key: f"/api/projects/{project_id}/download/{key}" for key in paths})
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/projects/<project_id>/download/<format_name>")
def download_export(project_id: str, format_name: str):
    paths = render_exports(load_project(project_id))
    if format_name not in paths:
        return jsonify(error="不支持的导出格式"), 404
    return send_file(paths[format_name], as_attachment=True)


@app.get("/api/projects/<project_id>/download/minutes")
def download_minutes(project_id: str):
    project = load_project(project_id)
    path = Path(project.get("minutes_path", ""))
    if not path.is_file():
        return jsonify(error="当前项目还没有会议纪要。"), 404
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False)
