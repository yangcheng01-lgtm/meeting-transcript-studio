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
SUMMARY_SKILLS_DIR = Path(os.environ.get("SUMMARY_SKILLS_DIR", str(APP_DIR / "skills")))
DEFAULT_SUMMARY_SKILL = os.environ.get("DEFAULT_SUMMARY_SKILL", "meeting-minutes-synthesis-zh")
DEFAULT_LLM_MODEL = os.environ.get("DEFAULT_LLM_MODEL", "external/glm-5.3-flash")
LLM_DIRECT_CHAR_LIMIT = int(os.environ.get("LLM_DIRECT_CHAR_LIMIT", "60000"))
LLM_CHUNK_CHAR_LIMIT = int(os.environ.get("LLM_CHUNK_CHAR_LIMIT", "22000"))
MAX_CHUNK_SECONDS = 300
LOCAL_ASR_MODEL = os.environ.get("LOCAL_ASR_MODEL", "small")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB local upload cap
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.RLock()
# LLM configuration is intentionally process-memory only. It is never persisted in projects.
_llm_config: dict[str, str] = {"api_url": "https://ai-service.segway-ninebot.com", "api_key": "", "model": DEFAULT_LLM_MODEL}
_runtime_asr_config: dict[str, str] = {}


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


def normalize_glossary(value: Any) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[\r\n,，;；]+", value)
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        items = []
    result = []
    for item in items:
        term = str(item).strip()
        if term and term not in result:
            result.append(term[:120])
    return result[:200]


def glossary_prompt(project: dict[str, Any]) -> str:
    return "、".join(normalize_glossary(project.get("glossary")))


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


def speaker_display_order(segments: list[dict]) -> list[str]:
    """按第一次发言时间排序，避免 SPEAKER_00 等内部编号误导人工标注。"""
    order: list[str] = []
    for item in sorted(segments, key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0)), str(x.get("speaker", "")))):
        speaker = str(item.get("speaker", "UNKNOWN"))
        if speaker not in order:
            order.append(speaker)
    return order


def friendly_speaker_name(speaker: str, index: int) -> str:
    return "未知发言人" if speaker == "UNKNOWN" else f"发言人{index + 1}"


def make_speaker_map(segments: list[dict], existing: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    palette = ["#e05a33", "#1b9aaa", "#d99e28", "#7357d8", "#5f9e5e", "#bc4e84", "#c76f21"]
    mapping = existing or {}
    for index, speaker in enumerate(speaker_display_order(segments)):
        default_name = friendly_speaker_name(speaker, index)
        mapping.setdefault(speaker, {"name": default_name, "role": "", "color": palette[index % len(palette)]})
        # 兼容旧项目：把 SPEAKER_00 / SPEAKER_01 这类占位名升级为“发言人1”。
        current_name = str(mapping.get(speaker, {}).get("name", "")).strip()
        if not current_name or re.fullmatch(r"SPEAKER[_\s-]*\d+", current_name, flags=re.IGNORECASE):
            mapping[speaker]["name"] = default_name
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


def parse_progress_line(line: str) -> dict[str, Any] | None:
    prefix = "PROGRESS_JSON "
    if not line.startswith(prefix):
        return None
    try:
        payload = json.loads(line[len(prefix):])
        total = max(1, int(payload.get("total") or 1))
        completed = max(0, min(total, int(payload.get("completed") or 0)))
        return {"step_name": str(payload.get("step_name") or "处理中"), "completed": completed, "total": total}
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def run_diarization(project_id: str, job_id: str, token: str | None, offline: bool = False, parent_job_id: str | None = None) -> None:
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
        local_pyannote_cache = DATA_ROOT / "model-cache" / "torch" / "pyannote"
        if local_pyannote_cache.is_dir():
            env.setdefault("PYANNOTE_CACHE", str(local_pyannote_cache))
        update_job(job_id, message="正在识别发言人…")
        command = [str(VOICEID_PYTHON), str(DIARIZE_SCRIPT), str(audio), "-o", str(out)]
        if offline:
            command.append("--offline")
        expected = project.get("expected_speakers")
        if expected:
            command.extend(["--num-speakers", str(int(expected))])
        process = subprocess.Popen(
            command,
            cwd=str(WORKSPACE), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def read_stderr() -> None:
            if process.stderr is not None:
                stderr_parts.append(process.stderr.read())

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        assert process.stdout is not None
        for raw_line in process.stdout:
            stdout_parts.append(raw_line)
            progress = parse_progress_line(raw_line.strip())
            if not progress:
                continue
            total, completed = progress["total"], progress["completed"]
            percent = round(completed * 100 / total, 1)
            message = f"正在识别发言人 · {progress['step_name']} · {completed}/{total}（{percent}%）"
            update_job(job_id, progress=percent, completed=completed, total=total, step=progress["step_name"], message=message)
            if parent_job_id:
                update_job(parent_job_id, progress=round(percent * 0.5, 1), message=message)
        process.wait()
        stderr_thread.join(timeout=2)
        stderr_text = "".join(stderr_parts)
        stdout_text = "".join(stdout_parts)
        if process.returncode != 0:
            raise RuntimeError(stderr_text[-1800:] or stdout_text[-1800:] or "识别发言人失败")
        diar_file = out / "diarization.json"
        segments = normalize_diarization(read_json(diar_file))
        project = load_project(project_id)
        project["diarization_segments"] = segments
        project["speaker_map"] = make_speaker_map(segments, project.get("speaker_map"))
        project["diarization_source"] = str(diar_file)
        save_project(project)
        update_job(job_id, status="done", message=f"发言人识别完成，共 {len(segments)} 个发言片段", finished_at=now())
    except Exception as exc:
        job_error(job_id, exc)


def load_asr_config() -> dict[str, Any]:
    # The model settings dialog now owns both LLM and ASR runtime credentials.
    # If the user has configured an API key in the UI, use it for qwen3-asr as well.
    if _runtime_asr_config.get("api_key"):
        return dict(_runtime_asr_config)
    if _llm_config.get("api_key"):
        return {
            "api_url": f"{normalize_llm_base_url(_llm_config.get('api_url'))}/v1/audio/transcriptions",
            "api_key": _llm_config["api_key"],
            "model": os.environ.get("ASR_MODEL", "qwen3-asr"),
            "language": os.environ.get("ASR_LANGUAGE", "zh"),
        }
    if ASR_CONFIG.is_file():
        return read_json(ASR_CONFIG)
    return {
        "api_url": os.environ.get("ASR_API_URL", "https://ai-service.segway-ninebot.com/v1/audio/transcriptions"),
        "api_key": os.environ.get("ASR_API_KEY", ""),
        "model": os.environ.get("ASR_MODEL", "qwen3-asr"),
        "language": os.environ.get("ASR_LANGUAGE", "zh"),
    }


def asr_request(audio_path: Path, config: dict[str, Any], offset: float, language: str | None, prompt: str = "") -> tuple[list[dict], str, str]:
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
    prompt = str(prompt or "").strip()
    prompted = {**base, "prompt": prompt[:1800]} if prompt else base

    candidates = [
        {**prompted, "response_format": "verbose_json", "timestamp_granularities[]": "segment"},
        {**prompted, "response_format": "verbose_json"},
        prompted,
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
                    timeout=120,
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
        config = load_asr_config()
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
            segments, text, quality = asr_request(chunk, config, pos, language, glossary_prompt(project))
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


def asr_with_retries(audio_path: Path, config: dict[str, Any], offset: float, language: str | None, prompt: str, attempts: int = 3) -> tuple[list[dict], str, str]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return asr_request(audio_path, config, offset, language, prompt)
        except Exception as exc:
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {400, 401, 403, 404, 422} or attempt >= attempts:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError(str(last_error or "qwen3-asr 调用失败"))


def speaker_block_cache_key(block: dict[str, Any], language: str | None, model: str, glossary: str) -> str:
    return "|".join([
        f"{float(block['start']):.3f}", f"{float(block['end']):.3f}", str(block["speaker"]),
        str(language or "auto"), str(model), glossary,
    ])


def run_qwen_speaker_aware_asr(project_id: str, job_id: str, language: str | None = "zh", parent_job_id: str | None = None) -> None:
    """qwen3-asr text + local pyannote boundaries, with retry and resume cache."""
    try:
        config = load_asr_config()
        if not config.get("api_key"):
            raise FileNotFoundError("未配置 qwen3-asr：请设置 ASR_CONFIG_PATH 或 ASR_API_KEY。")
        project = load_project(project_id)
        diarization = project.get("diarization_segments", [])
        if not diarization:
            raise RuntimeError("请先识别发言人，才能转写文字。")
        update_job(job_id, status="running", message="正在按发言人切分音频…")
        audio = media_to_wav(project)
        if not FFMPEG.is_file():
            raise FileNotFoundError(f"未找到 ffmpeg：{FFMPEG}")
        blocks = build_speaker_asr_blocks(diarization)
        if not blocks:
            raise RuntimeError("识别出的发言人时间线中没有可转写的有效发言")
        chunks_dir = project_dir(project_id) / "qwen_speaker_chunks"
        chunks_dir.mkdir(exist_ok=True)
        cache_path = project_dir(project_id) / "qwen_speaker_cache.json"
        cache = read_json(cache_path) if cache_path.is_file() else {"version": 1, "entries": {}}
        if not isinstance(cache.get("entries"), dict):
            cache = {"version": 1, "entries": {}}
        glossary = glossary_prompt(project)
        transcript, raw_texts = [], []
        cache_hits = 0
        model = str(config.get("model", "qwen3-asr"))
        for index, block in enumerate(blocks, 1):
            key = speaker_block_cache_key(block, language, model, glossary)
            cached = cache["entries"].get(key) or {}
            text = str(cached.get("text", "")).strip()
            percent = round(index * 100 / len(blocks), 1)
            if text:
                cache_hits += 1
                message = f"恢复已完成片段：第 {index}/{len(blocks)} 段（缓存 {cache_hits}；{percent}%）"
                update_job(job_id, progress=percent, completed=index, total=len(blocks), message=message)
                if parent_job_id:
                    update_job(parent_job_id, progress=round(50 + percent * 0.5, 1), message=message)
            else:
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
                message = f"正在转写文字（{language_label}）：第 {index}/{len(blocks)} 段，完成 {percent}%，失败自动重试"
                update_job(job_id, progress=percent, completed=index, total=len(blocks), message=message)
                if parent_job_id:
                    update_job(parent_job_id, progress=round(50 + percent * 0.5, 1), message=message)
                response_segments = []
                text = ""
                try:
                    response_segments, text, _ = asr_with_retries(chunk, config, block["start"], language, glossary)
                except Exception as seg_exc:
                    text = ""
                    update_job(job_id, message=f"第 {index}/{len(blocks)} 段转写失败（{seg_exc}），跳过继续")
                if not text.strip() and response_segments:
                    text = " ".join(str(item.get("text", "")) for item in response_segments).strip()
                text = text.strip()
                if text:
                    cache["entries"][key] = {
                        "start": round(block["start"], 3), "end": round(block["end"], 3),
                        "speaker": block["speaker"], "text": text, "updated_at": now(),
                    }
                    write_json(cache_path, cache)
            if text:
                transcript.append({
                    "start": round(block["start"], 3), "end": round(block["end"], 3),
                    "text": text, "speaker_hint": block["speaker"], "timing_quality": "speaker_block",
                })
                raw_texts.append(text)
        if not transcript:
            raise RuntimeError("qwen3-asr 未返回可用文字")
        project = load_project(project_id)
        project["asr_segments"] = transcript
        project["asr_raw_text"] = "\n".join(raw_texts)
        project["asr_timing_quality"] = "speaker_block"
        project["asr_source"] = f"company qwen3-asr + local pyannote speaker blocks; requested_language={language or 'auto'}"
        project["transcript_segments"] = [
            {"id": uuid.uuid4().hex[:10], "start": item["start"], "end": item["end"],
             "speaker": item["speaker_hint"], "text": item["text"], "timing_quality": "speaker_block"}
            for item in transcript
        ]
        project["qwen_cache_path"] = str(cache_path)
        project["qwen_cache_hits"] = cache_hits
        save_project(project)
        update_job(job_id, status="done", progress=100, completed=len(blocks), total=len(blocks), message=f"转写完成：{len(transcript)} 段文字，复用缓存 {cache_hits} 段", finished_at=now())
    except Exception as exc:
        job_error(job_id, exc)

def run_recommended_pipeline(project_id: str, job_id: str, language: str | None = "zh") -> None:
    """Recommended sequential pipeline: offline diarization → qwen speaker-aware ASR."""
    try:
        update_job(job_id, status="running", message="正在识别发言人和转写文字…")
        project = load_project(project_id)
        media_to_wav(project)
        # Reuse existing speaker timeline when possible; only run diarization if absent.
        has_diarization = bool(project.get("diarization_segments"))
        if has_diarization:
            update_job(job_id, progress=10, message="已有发言人识别结果，直接转写文字…")
        else:
            diar_job = create_job(project_id, "diarization_offline")
            run_diarization(project_id, diar_job, None, True, parent_job_id=job_id)
            if _jobs[diar_job].get("status") != "done":
                raise RuntimeError(_jobs[diar_job].get("message", "识别发言人失败"))
        update_job(job_id, progress=50, message="发言人识别完成，开始转写文字…")
        qwen_job = create_job(project_id, "asr_qwen3_speaker_aware")
        run_qwen_speaker_aware_asr(project_id, qwen_job, language, parent_job_id=job_id)
        if _jobs[qwen_job].get("status") != "done":
            raise RuntimeError(_jobs[qwen_job].get("message", "转写文字失败"))
        project = load_project(project_id)
        project["transcript_segments"] = align_segments(project["asr_segments"], project["diarization_segments"])
        save_project(project)
        update_job(job_id, status="done", progress=100, message=f"识别完成：{len(project['transcript_segments'])} 段带发言人逐字稿", finished_at=now())
    except Exception as exc:
        job_error(job_id, exc)


def run_speaker_count_correction(project_id: str, job_id: str) -> None:
    """按人工确认的人数重跑 Speaker 分离；如已有逐字稿且模型可用，则同步更新署名。"""
    try:
        update_job(job_id, status="running", message="正在按确认的人数重新识别发言人…")
        media_to_wav(load_project(project_id))
        diar_job = create_job(project_id, "diarization_correction")
        run_diarization(project_id, diar_job, None, True, parent_job_id=job_id)
        if _jobs[diar_job].get("status") != "done":
            raise RuntimeError(_jobs[diar_job].get("message", "重新识别发言人失败"))

        project = load_project(project_id)
        if not (project.get("transcript_segments") or project.get("asr_segments")):
            update_job(job_id, status="done", progress=100, message="说话人已修正；尚未生成逐字稿，可点击“开始识别”继续。", finished_at=now())
            return

        config = load_asr_config()
        if not config.get("api_key"):
            update_job(job_id, status="done", progress=100, message="说话人已修正；未检测到 API Key，请配置模型后重新识别更新逐字稿。", finished_at=now())
            return

        update_job(job_id, progress=50, message="发言人已修正，正在更新逐字稿…")
        qwen_job = create_job(project_id, "asr_qwen3_speaker_aware")
        run_qwen_speaker_aware_asr(project_id, qwen_job, None, parent_job_id=job_id)
        if _jobs[qwen_job].get("status") != "done":
            raise RuntimeError(_jobs[qwen_job].get("message", "更新逐字稿失败"))
        project = load_project(project_id)
        project["transcript_segments"] = align_segments(project["asr_segments"], project["diarization_segments"])
        save_project(project)
        update_job(job_id, status="done", progress=100, message=f"发言人已修正：{len(project['transcript_segments'])} 段逐字稿已更新", finished_at=now())
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


def summary_skill_metadata(skill_id: str, path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    name_match = re.search(r"(?m)^name:\s*([^\n]+)", text)
    heading_match = re.search(r"(?m)^#\s+(.+)$", text)
    description_match = re.search(r"(?ms)^description:\s*>?\s*(.+?)(?=\n[a-zA-Z][\w-]*:|\n---)", text)
    title = heading_match.group(1).strip() if heading_match else skill_id
    description = re.sub(r"\s+", " ", description_match.group(1)).strip() if description_match else ""
    return {"id": skill_id, "name": (name_match.group(1).strip() if name_match else skill_id), "title": title, "description": description[:220]}


def available_summary_skills() -> list[dict[str, str]]:
    skills = []
    if SUMMARY_SKILLS_DIR.is_dir():
        for directory in sorted(SUMMARY_SKILLS_DIR.iterdir()):
            skill_file = directory / "SKILL.md"
            if directory.is_dir() and skill_file.is_file() and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,80}", directory.name):
                skills.append(summary_skill_metadata(directory.name, skill_file))
    return skills


def resolve_summary_skill(skill_id: str) -> tuple[dict[str, str], Path]:
    skill_id = str(skill_id or DEFAULT_SUMMARY_SKILL).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,80}", skill_id):
        raise ValueError("总结 Skill 名称不合法。")
    path = SUMMARY_SKILLS_DIR / skill_id / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(f"未找到总结 Skill：{skill_id}")
    return summary_skill_metadata(skill_id, path), path


def split_transcript_for_llm(transcript: str, max_chars: int = LLM_CHUNK_CHAR_LIMIT) -> list[str]:
    chunks, current = [], []
    size = 0
    for line in transcript.splitlines():
        extra = len(line) + 1
        if current and size + extra > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def report_context(project: dict[str, Any]) -> str:
    terms = glossary_prompt(project)
    return f"""项目标题：{project.get('title', '未命名项目')}
项目编号：{project.get('project_code', project.get('id', '未提供'))}
来源文件：{json.dumps(project.get('media_paths') or [project.get('media_path')], ensure_ascii=False)}
业务术语词表：{terms or '未提供'}"""


def llm_report_prompt(project: dict[str, Any], skill_id: str, source_text: str | None = None) -> tuple[str, str, dict[str, str]]:
    metadata, skill_path = resolve_summary_skill(skill_id)
    skill = skill_path.read_text(encoding="utf-8")
    transcript = source_text if source_text is not None else transcript_for_agent(project)
    system = f"""你是多听工作台的音视频总结 Agent。请严格遵守下面的 Skill 规则。

{skill}

只输出最终中文 Markdown 交付物，不要解释你使用了哪些规则，不要输出 JSON。"""
    user = f"""请根据下面带 Speaker、带时间戳的逐字稿生成“{metadata['title']}”。

{report_context(project)}

逐字稿或分段事实笔记：
{transcript}

统一要求：
1. 保留关键数字、单位、人物归属、时间戳和不确定性口径。
2. 严格区分原文事实、明确结论和分析推断。
3. 未明确的负责人、日期、型号、结论不得臆造。
4. 结合业务术语词表修正明显 ASR 同音错词；无法确认时标记“待复核”。
5. 输出应可直接审改，并能追溯到原始时间戳。"""
    return system, user, metadata


def llm_chat(system: str, user: str, temperature: float = 0.2) -> str:
    import requests
    body = {
        "model": _llm_config.get("model"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
    }
    response = requests.post(llm_endpoint("chat/completions"), headers=llm_headers(), json=body, timeout=900)
    response.raise_for_status()
    return extract_chat_content(response.json())


def validate_named_speakers(project: dict[str, Any]) -> None:
    """总结和导出都要求真实姓名；角色仅是增强信息，不强制填写。"""
    unresolved = []
    for speaker in sorted({str(item.get("speaker", "UNKNOWN")) for item in project.get("transcript_segments", []) if str(item.get("speaker", "UNKNOWN")) != "UNKNOWN"}):
        name = str(project.get("speaker_map", {}).get(speaker, {}).get("name", "")).strip()
        if not name or name == speaker or re.fullmatch(r"SPEAKER[_\s-]*\d+", name, flags=re.IGNORECASE) or re.fullmatch(r"发言人\d+", name):
            unresolved.append(speaker)
    if unresolved:
        labels = [speaker_label(project, speaker) for speaker in unresolved]
        raise ValueError(f"请先为以下发言人填写姓名：{'、'.join(labels)}。角色可以留空。")


def generate_report_content(project: dict[str, Any], skill_id: str) -> tuple[str, dict[str, str], int]:
    validate_named_speakers(project)
    transcript = transcript_for_agent(project)
    if len(transcript) <= LLM_DIRECT_CHAR_LIMIT:
        system, user, metadata = llm_report_prompt(project, skill_id, transcript)
        return llm_chat(system, user), metadata, 1
    chunks = split_transcript_for_llm(transcript)
    notes = []
    glossary = glossary_prompt(project)
    for index, chunk in enumerate(chunks, 1):
        system = "你是长音视频逐字稿的事实抽取器。只抽取本段明确事实、数字、人物观点、决定、行动项、风险、开放问题和重要术语；每条保留时间戳，不要臆造，不要写最终总结。"
        user = f"分段 {index}/{len(chunks)}。业务术语：{glossary or '未提供'}。\n\n{chunk}"
        notes.append(f"## 分段 {index}/{len(chunks)}\n" + llm_chat(system, user, 0.1))
    system, user, metadata = llm_report_prompt(project, skill_id, "\n\n".join(notes))
    return llm_chat(system, user), metadata, len(chunks) + 1


def llm_minutes_prompt(project: dict[str, Any]) -> tuple[str, str]:
    system, user, _ = llm_report_prompt(project, DEFAULT_SUMMARY_SKILL)
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
        if not name or name == speaker or re.fullmatch(r"SPEAKER[_\s-]*\d+", name, flags=re.IGNORECASE) or re.fullmatch(r"发言人\d+", name):
            unresolved.append(speaker)
    if unresolved:
        raise ValueError(f"请先在“确认人名”中填写：{'、'.join(speaker_label(project, speaker) for speaker in unresolved)}。")
    export_dir = project_dir(project["id"]) / "exports"
    export_dir.mkdir(exist_ok=True)
    slug = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", project.get("title", "逐字稿"))[:60] or "逐字稿"
    md_path, txt_path, srt_path = export_dir / f"{slug}_带署名逐字稿.md", export_dir / f"{slug}_带署名逐字稿.txt", export_dir / f"{slug}_带署名逐字稿.srt"
    md = [f"# {project.get('title', '逐字稿')} — 带署名逐字稿", "", f"- 生成时间：{now()}", "- 注：说话人边界由本地 pyannote 分离生成；姓名/角色由人工确认。", ""]
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
    _runtime_asr_config.update({
        "api_url": f"{api_url}/v1/audio/transcriptions", "api_key": api_key,
        "model": os.environ.get("ASR_MODEL", "qwen3-asr"), "language": os.environ.get("ASR_LANGUAGE", "zh"),
    })
    return jsonify({"api_url": api_url, "model": model, "configured": True, "asr_configured": True})


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


@app.get("/api/summary-skills")
def list_summary_skills():
    return jsonify({"default": DEFAULT_SUMMARY_SKILL, "skills": available_summary_skills()})


def generate_and_save_report(project_id: str, skill_id: str) -> dict[str, Any]:
    project = load_project(project_id)
    content, metadata, request_count = generate_report_content(project, skill_id)
    exports = project_dir(project_id) / "exports"
    exports.mkdir(exist_ok=True)
    code = project.get("project_code") or project.get("id")
    title_slug = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", metadata["title"])[:50] or skill_id
    report_path = exports / f"{code}__OUT__{title_slug}.md"
    report_path.write_text(content + ("\n" if not content.endswith("\n") else ""), encoding="utf-8")
    reports = project.get("reports") if isinstance(project.get("reports"), dict) else {}
    reports[skill_id] = {
        "path": str(report_path), "title": metadata["title"], "model": _llm_config.get("model"),
        "generated_at": now(), "llm_requests": request_count,
    }
    project["reports"] = reports
    project["latest_report_skill"] = skill_id
    project["latest_report_path"] = str(report_path)
    if skill_id == DEFAULT_SUMMARY_SKILL:
        project["minutes_path"] = str(report_path)
        project["minutes_model"] = _llm_config.get("model")
        project["minutes_generated_at"] = now()
    save_project(project)
    return {
        "path": f"/api/projects/{project_id}/download/report/{skill_id}",
        "skill": skill_id, "title": metadata["title"], "model": _llm_config.get("model"),
        "llm_requests": request_count, "project": public_project(project),
    }


@app.post("/api/projects/<project_id>/generate-report")
def generate_report(project_id: str):
    payload = request.get_json(silent=True) or {}
    skill_id = str(payload.get("skill") or DEFAULT_SUMMARY_SKILL)
    job_id = create_job(project_id, "summary_report")

    def worker() -> None:
        try:
            update_job(job_id, status="running", progress=5, message="准备生成总结报告…")
            result = generate_and_save_report(project_id, skill_id)
            update_job(job_id, status="done", progress=100, result=result, message=f"总结报告已生成：{result['title']}", finished_at=now())
        except Exception as exc:
            job_error(job_id, exc)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify(_jobs[job_id]), 202


@app.post("/api/projects/<project_id>/generate-minutes")
def generate_minutes(project_id: str):
    try:
        return jsonify(generate_and_save_report(project_id, DEFAULT_SUMMARY_SKILL))
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
        "glossary": normalize_glossary(payload.get("glossary")),
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
    glossary = normalize_glossary(request.form.get("glossary", ""))
    project = {
        "id": project_id, "title": title, "created_at": now(), "updated_at": now(),
        "media_path": str(sources[0]), "media_paths": [str(source) for source in sources],
        "source_count": len(sources), "audio_path": "", "file_manifest": [],
        "diarization_segments": [], "asr_segments": [], "asr_raw_text": "",
        "asr_timing_quality": "", "transcript_segments": [], "speaker_map": {},
        "expected_speakers": expected_speakers,
        "glossary": glossary,
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
        output = folder / "source_%(id)s.%(ext)s"
        try:
            import yt_dlp
            options = {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": str(output), "noplaylist": True, "quiet": True,
                "no_warnings": True, "restrictfilenames": True,
            }
            if os.environ.get("YTDLP_PROXY", "").strip():
                options["proxy"] = os.environ["YTDLP_PROXY"].strip()
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
                prepared = downloader.prepare_filename(info)
            source = Path(prepared)
        except ModuleNotFoundError:
            yt_python = os.environ.get("YTDLP_PYTHON", "").strip()
            prefix = [yt_python] if yt_python else (["py", "-3.12"] if shutil.which("py") else [sys.executable])
            proxy_args = ["--proxy", os.environ["YTDLP_PROXY"].strip()] if os.environ.get("YTDLP_PROXY", "").strip() else []
            process = subprocess.run(
                [*prefix, "-m", "yt_dlp", "--no-playlist", "--quiet", "--no-warnings", "--restrict-filenames",
                 *proxy_args, "--print-json", "-f", "bestaudio[ext=m4a]/bestaudio/best", "-o", str(output), url],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
            )
            if process.returncode != 0:
                raise RuntimeError(process.stderr[-1600:] or "yt-dlp 命令行下载失败")
            lines = [line for line in process.stdout.splitlines() if line.strip().startswith("{")]
            info = json.loads(lines[-1]) if lines else {}
            source = Path(str(info.get("_filename") or ""))
        if not source.is_file():
            candidates = sorted(folder.glob("source_*"), key=lambda path: path.stat().st_mtime, reverse=True)
            source = candidates[0] if candidates else source
        if not source.is_file():
            raise RuntimeError("YouTube 音频下载完成但未找到本地文件")
        original_source = source
        clip_start = max(0.0, float(payload.get("clip_start") or 0))
        clip_duration = float(payload.get("clip_duration") or 0)
        if clip_duration:
            if not 5 <= clip_duration <= 600:
                raise ValueError("YouTube 截取时长需在 5–600 秒之间。")
            if not FFMPEG.is_file():
                raise FileNotFoundError(f"未找到 ffmpeg：{FFMPEG}")
            clipped = folder / f"source_{str(info.get('id') or 'youtube')}_clip_{int(clip_start)}s_{int(clip_duration)}s.wav"
            cut = subprocess.run(
                [str(FFMPEG), "-y", "-ss", str(clip_start), "-t", str(clip_duration), "-i", str(original_source), "-ar", "16000", "-ac", "1", str(clipped)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if cut.returncode != 0:
                raise RuntimeError(cut.stderr[-1600:] or "YouTube 音频截取失败")
            source = clipped
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
            "youtube_original_media_path": str(original_source),
            "youtube_clip_start": clip_start,
            "youtube_clip_duration": clip_duration or None,
            "diarization_segments": [],
            "asr_segments": [],
            "asr_raw_text": "",
            "asr_timing_quality": "",
            "transcript_segments": [],
            "speaker_map": {},
            "expected_speakers": expected_speakers,
            "glossary": normalize_glossary(payload.get("glossary")),
        }
        save_project(project)
        return jsonify(public_project(project)), 201
    except Exception as exc:
        # Keep a failed project out of the normal list but retain the diagnostic file.
        (folder / "youtube_import_error.txt").write_text(str(exc), encoding="utf-8")
        return jsonify(error=f"YouTube 导入失败：{exc}"), 400


@app.post("/api/projects/demo")
def load_demo():
    candidates = [DATA_ROOT / "source-materials" / "会话总结_0826", WORKSPACE / "会话总结_0826"]
    base = next((path for path in candidates if path.is_dir()), candidates[0])
    media = base / "新录音 8_audio.wav"
    diar = base / "diar_audio8" / "diarization.json"
    text = base / "audio8_transcript.txt"
    if not media.is_file() or not diar.is_file():
        return jsonify(error="未找到当前会话示例文件。"), 404
    project_id = uuid.uuid4().hex[:10]
    segments = normalize_diarization(read_json(diar))
    project = {"id": project_id, "title": "新录音 8 — 访谈", "created_at": now(), "updated_at": now(), "media_path": str(media), "audio_path": str(media), "diarization_segments": segments, "diarization_source": str(diar), "asr_segments": [], "asr_raw_text": "", "asr_timing_quality": "", "transcript_segments": [], "speaker_map": make_speaker_map(segments), "glossary": [], "reference_text": text.read_text(encoding="utf-8", errors="replace") if text.is_file() else ""}
    project_dir(project_id).mkdir(parents=True, exist_ok=True)
    save_project(project)
    return jsonify(public_project(project)), 201


@app.get("/api/projects/<project_id>")
def get_project(project_id: str):
    try:
        return jsonify(public_project(load_project(project_id)))
    except FileNotFoundError:
        return jsonify(error="项目不存在"), 404


@app.put("/api/projects/<project_id>/expected-speakers")
def update_expected_speakers(project_id: str):
    try:
        payload = request.get_json(silent=True) or {}
        raw = str(payload.get("expected_speakers", "")).strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 20:
            raise ValueError("说话人数需为 1–20 的整数。")
        expected = int(raw)
        project = load_project(project_id)
        project["expected_speakers"] = expected
        save_project(project)
        return jsonify(public_project(project))
    except Exception as exc:
        return jsonify(error=str(exc)), 400


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


@app.post("/api/projects/<project_id>/process/correct-speakers")
def start_speaker_count_correction(project_id: str):
    try:
        payload = request.get_json(silent=True) or {}
        raw = str(payload.get("expected_speakers", "")).strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 20:
            return jsonify(error="实际说话人数需为 1–20 的整数。"), 400
        expected = int(raw)
        project = load_project(project_id)
        project["expected_speakers"] = expected
        save_project(project)
        job_id = create_job(project_id, "speaker_count_correction")
        threading.Thread(target=run_speaker_count_correction, args=(project_id, job_id), daemon=True).start()
        return jsonify(_jobs[job_id]), 202
    except FileNotFoundError:
        return jsonify(error="项目不存在"), 404
    except Exception as exc:
        return jsonify(error=str(exc)), 400


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


@app.put("/api/projects/<project_id>/glossary")
def update_glossary(project_id: str):
    try:
        payload = request.get_json(silent=True) or {}
        project = load_project(project_id)
        project["glossary"] = normalize_glossary(payload.get("glossary"))
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


@app.get("/api/projects/<project_id>/download/report/<skill_id>")
def download_report(project_id: str, skill_id: str):
    project = load_project(project_id)
    reports = project.get("reports") if isinstance(project.get("reports"), dict) else {}
    path = Path(str((reports.get(skill_id) or {}).get("path", "")))
    if not path.is_file():
        return jsonify(error="当前项目还没有该类型报告。"), 404
    return send_file(path, as_attachment=True)


@app.get("/api/projects/<project_id>/download/minutes")
def download_minutes(project_id: str):
    project = load_project(project_id)
    path = Path(project.get("minutes_path", ""))
    if not path.is_file():
        return jsonify(error="当前项目还没有会议纪要。"), 404
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False)
