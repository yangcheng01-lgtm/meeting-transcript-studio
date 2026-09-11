"""本地说话人分离（speaker diarization）

环境：D:\\voiceid31（pyannote.audio 3.1.1 + torch/torchaudio 2.5.1 CPU）
示例：
  D:\\voiceid31\\Scripts\\python.exe diarize.py "会话总结_0826\\video(28)_audio.wav" -o "会话总结_0826\\diar_video28"

令牌：优先读取环境变量 HF_TOKEN；也支持 --token hf_xxx。
说明：本脚本输出 SPEAKER_00、SPEAKER_01 等匿名说话人标签。要映射为具体姓名，需以
声音样本进行声纹注册/比对或由人工核对少量片段。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

MODEL_ID = "pyannote/speaker-diarization-3.1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="pyannote 本地说话人分离")
    parser.add_argument("audio", help="输入音频文件；建议使用 16 kHz 单声道 WAV")
    parser.add_argument("-o", "--out", default="diarization_out", help="输出目录")
    parser.add_argument("--token", help="Hugging Face token；未提供时读取环境变量 HF_TOKEN")
    parser.add_argument("--offline", action="store_true", help="仅使用已下载的本地 Hugging Face 模型缓存，不访问网络")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--num-speakers", type=int, help="已知且确定说话人数时使用")
    group.add_argument("--max-speakers", type=int, help="已知说话人数量上限时使用")
    parser.add_argument("--min-speakers", type=int, help="说话人数量下限（可与 --max-speakers 联用）")
    return parser.parse_args()


def iter_segments(diarization):
    """把 pyannote Annotation 转为稳定、可序列化的说话人片段。"""
    annotation = getattr(diarization, "speaker_diarization", diarization)
    segments = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        segments.append(
            {
                "start": round(float(turn.start), 3),
                "end": round(float(turn.end), 3),
                "speaker": str(speaker),
            }
        )
    return sorted(segments, key=lambda item: (item["start"], item["end"], item["speaker"]))


def write_outputs(audio: Path, out_dir: Path, diarization, segments: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "diarization.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "audio": str(audio.resolve()),
                "model": MODEL_ID,
                "segments": segments,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    rttm_path = out_dir / "diarization.rttm"
    with rttm_path.open("w", encoding="utf-8") as f:
        for item in segments:
            duration = item["end"] - item["start"]
            f.write(
                f"SPEAKER {audio.name} 1 {item['start']:.3f} {duration:.3f} "
                f"<NA> <NA> {item['speaker']} <NA> <NA>\n"
            )

    summary = {}
    for item in segments:
        summary[item["speaker"]] = summary.get(item["speaker"], 0.0) + item["end"] - item["start"]

    summary_path = out_dir / "说话人时长汇总.md"
    with summary_path.open("w", encoding="utf-8") as f:
        f.write("# 说话人分离结果\n\n")
        f.write(f"- 音频：`{audio.resolve()}`\n")
        f.write(f"- 模型：`{MODEL_ID}`\n\n")
        f.write("| 匿名说话人 | 累计发言时长 |\n|---|---:|\n")
        for speaker, duration in sorted(summary.items(), key=lambda item: -item[1]):
            minutes, seconds = divmod(round(duration), 60)
            f.write(f"| {speaker} | {minutes}分{seconds:02d}秒 |\n")

    print(f"\n完成：识别到 {len(summary)} 位匿名说话人、{len(segments)} 个发言片段。")
    for speaker, duration in sorted(summary.items(), key=lambda item: -item[1]):
        print(f"  {speaker}: {duration:.1f}s")
    print("输出目录：", out_dir.resolve())
    print("  - diarization.json（方便与转写时间戳对齐）")
    print("  - diarization.rttm（标准说话人分离格式）")
    print("  - 说话人时长汇总.md")


def main() -> None:
    args = parse_args()
    audio = Path(args.audio)
    if not audio.is_file():
        raise SystemExit(f"文件不存在：{audio}")

    token = args.token or os.environ.get("HF_TOKEN")
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    if not token and not args.offline:
        raise SystemExit("缺少 Hugging Face token：设置环境变量 HF_TOKEN / 传入 --token，或在模型已缓存时加 --offline。")
    if args.num_speakers and args.num_speakers < 1:
        raise SystemExit("--num-speakers 必须大于 0。")
    if args.min_speakers and args.min_speakers < 1:
        raise SystemExit("--min-speakers 必须大于 0。")
    if args.max_speakers and args.max_speakers < 1:
        raise SystemExit("--max-speakers 必须大于 0。")
    if args.min_speakers and args.max_speakers and args.min_speakers > args.max_speakers:
        raise SystemExit("--min-speakers 不能大于 --max-speakers。")

    print(f"[1/3] 加载 pipeline：{MODEL_ID}{'（离线缓存）' if args.offline else ''}")
    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(MODEL_ID, use_auth_token=token)
    print("[2/3] 执行全局说话人分离（CPU；长音频耗时较长）")

    kwargs = {}
    if args.num_speakers:
        kwargs["num_speakers"] = args.num_speakers
    else:
        if args.min_speakers:
            kwargs["min_speakers"] = args.min_speakers
        if args.max_speakers:
            kwargs["max_speakers"] = args.max_speakers

    diarization = pipeline(str(audio), **kwargs)
    segments = iter_segments(diarization)
    if not segments:
        raise SystemExit("未检测到有效说话人片段；请检查音频文件或音量。")

    print("[3/3] 写入标准化结果")
    write_outputs(audio, Path(args.out), diarization, segments)


if __name__ == "__main__":
    main()
