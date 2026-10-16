"""B流程：飞书妙记集成后端模块。

提供 lark-cli 调用封装和飞书妙记逐字稿解析，供 app.py 集成使用。
不直接导入 Flask；纯函数，方便测试。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def _lark_cmd() -> list[str]:
    """Return the correct lark-cli executable for this platform."""
    if sys.platform == "win32":
        return ["lark-cli.cmd"]
    return ["lark-cli"]


def run_lark_cli(args: list[str]) -> dict[str, Any]:
    """Execute a lark-cli command and return parsed JSON result."""
    cmd = _lark_cmd() + args + ["--format", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli 失败: {result.stderr[-500:]}")
    output = result.stdout.strip()
    if not output:
        return {}
    data = json.loads(output)
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(f"lark-cli 返回错误: {json.dumps(data.get('error', {}), ensure_ascii=False)}")
    return data


def upload_to_drive(audio_path: str) -> str:
    """Upload a local audio file to Feishu Drive; return file_token."""
    data = run_lark_cli(["drive", "+upload", "--file", audio_path, "--as", "user"])
    file_token = ""
    if isinstance(data, dict):
        file_token = data.get("file_token") or data.get("token") or ""
        if not file_token and isinstance(data.get("data"), dict):
            file_token = data["data"].get("file_token") or data["data"].get("token") or ""
    if not file_token:
        raise RuntimeError(f"Drive 上传未返回 file_token: {json.dumps(data, ensure_ascii=False)[:300]}")
    return file_token


def create_minute(file_token: str) -> tuple[str, str]:
    """Create a Feishu Minute from a Drive file_token; return (minute_token, minute_url)."""
    data = run_lark_cli(["minutes", "+upload", "--file-token", file_token, "--as", "user"])
    inner = data.get("data", data) if isinstance(data, dict) else {}
    minute_url = inner.get("minute_url", data.get("minute_url", ""))
    minute_token = inner.get("minute_token", data.get("minute_token", ""))
    if not minute_token and minute_url:
        minute_token = minute_url.rstrip("/").split("/")[-1].split("?")[0]
    if not minute_token:
        raise RuntimeError(f"妙记创建未返回 token: {json.dumps(data, ensure_ascii=False)[:300]}")
    return minute_token, minute_url


def fetch_minute_detail(minute_token: str, wait_ready: bool = True) -> dict[str, Any]:
    """Fetch minute artifacts: transcript + summary + todo + chapter + keyword."""
    args = [
        "minutes", "+detail",
        "--minute-tokens", minute_token,
        "--transcript", "--summary", "--todo", "--chapter", "--keyword",
        "--as", "user",
    ]
    if wait_ready:
        args.append("--wait-ready")
    data = run_lark_cli(args)
    inner = data.get("data", data) if isinstance(data, dict) else {}
    minutes = inner.get("minutes") or data.get("minutes") or []
    if isinstance(inner, list):
        minutes = inner
    if isinstance(data, list):
        minutes = data
    if not minutes:
        if inner.get("minute_token") or data.get("minute_token"):
            minutes = [inner if inner.get("minute_token") else data]
    if not minutes:
        raise RuntimeError(f"妙记详情未返回数据: {json.dumps(data, ensure_ascii=False)[:500]}")
    return minutes[0]


def read_transcript(transcript_path: str) -> str:
    """Read the transcript file, resolving relative paths against cwd."""
    transcript_file = Path(transcript_path) if transcript_path else Path("")
    if not transcript_file.is_absolute():
        transcript_file = Path.cwd() / transcript_file
    if not transcript_file.is_file():
        return ""
    return transcript_file.read_text(encoding="utf-8")


def parse_feishu_transcript(transcript_text: str) -> list[dict[str, Any]]:
    """Parse a Feishu Minutes transcript into transcript_segments.

    Feishu format:
        2026-10-16 23:57:51 CST|7min 41s
        Keywords: ...
        Speaker 1 00:00:00.070
        text here.
        Speaker 2 00:00:03.080
        more text.

    Returns list of {start, end, speaker, text, timing_quality} segments.
    """
    if not transcript_text:
        return []
    segments: list[dict[str, Any]] = []
    speaker_re = re.compile(r"^Speaker\s+(\d+)\s+(\d{2}:\d{2}:\d{2}\.\d{3})\s*$", re.MULTILINE)
    matches = list(speaker_re.finditer(transcript_text))
    for index, match in enumerate(matches):
        speaker_num = int(match.group(1))
        start_str = match.group(2)
        start = _timestamp_to_seconds(start_str)
        end = _timestamp_to_seconds(matches[index + 1].group(2)) if index + 1 < len(matches) else start + 30
        text_start = match.end()
        text_end = matches[index + 1].start() if index + 1 < len(matches) else len(transcript_text)
        text = transcript_text[text_start:text_end].strip()
        if not text:
            continue
        segments.append({
            "id": f"fs_{index:04d}",
            "start": round(start, 3),
            "end": round(end, 3),
            "speaker": f"SPEAKER_{speaker_num - 1:02d}",
            "text": text,
            "timing_quality": "feishu_minute",
        })
    return segments


def _timestamp_to_seconds(ts: str) -> float:
    """Convert HH:MM:SS.mmm to float seconds."""
    parts = ts.split(":")
    if len(parts) != 3:
        return 0.0
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_minute_speakers(transcript_text: str) -> dict[str, dict[str, str]]:
    """Extract speaker labels from a Feishu transcript.

    Returns a speaker_map like {"SPEAKER_00": {"name": "Speaker 1", "role": "", "color": "#e05a33"}}.
    """
    if not transcript_text:
        return {}
    speaker_re = re.compile(r"^Speaker\s+(\d+)\s+\d{2}:\d{2}:\d{2}\.\d{3}", re.MULTILINE)
    numbers = sorted({int(m.group(1)) for m in speaker_re.finditer(transcript_text)})
    palette = ["#e05a33", "#1b9aaa", "#d99e28", "#7357d8", "#5f9e5e", "#bc4e84", "#c76f21"]
    mapping: dict[str, dict[str, str]] = {}
    for index, num in enumerate(numbers):
        speaker_id = f"SPEAKER_{num - 1:02d}"
        mapping[speaker_id] = {
            "name": f"Speaker {num}",
            "role": "",
            "color": palette[index % len(palette)],
        }
    return mapping


def push_report_to_feishu_doc(report_path: str, folder_token: str | None = None, doc_name: str | None = None) -> dict[str, str]:
    """Push a local report MD file to a Feishu Doc.

    Uses lark-cli drive +upload to upload the file, then returns the doc link.
    """
    if not Path(report_path).is_file():
        raise FileNotFoundError(f"报告文件不存在: {report_path}")
    args = ["drive", "+upload", "--file", report_path, "--as", "user"]
    if folder_token:
        args += ["--folder-token", folder_token]
    if doc_name:
        args += ["--name", doc_name]
    data = run_lark_cli(args)
    inner = data.get("data", data) if isinstance(data, dict) else {}
    file_token = inner.get("file_token") or data.get("file_token", "")
    url = inner.get("url") or data.get("url", "")
    if not file_token:
        raise RuntimeError(f"云文档推送失败: {json.dumps(data, ensure_ascii=False)[:300]}")
    return {"file_token": file_token, "url": url}
