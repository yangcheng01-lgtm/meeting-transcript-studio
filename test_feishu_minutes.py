"""测试 feishu_minutes.py 的纯函数：逐字稿解析、说话人提取、时间戳转换。"""
from __future__ import annotations

import unittest

from feishu_minutes import (
    parse_feishu_transcript,
    parse_minute_speakers,
    _timestamp_to_seconds,
)


SAMPLE_TRANSCRIPT = """2026-10-16 23:57:51 CST|7min 41s

Keywords:
Europe version、battery capacity、high temperature

Speaker 1 00:00:00.070
做APC，就是如何做安全业务的。

Speaker 2 00:00:03.080
Free time to the force. Keep coming up with new ideas.

Speaker 3 00:00:17.030
The UX, I see the UX really as a part of the, how much capacity you have in operations.

Speaker 2 00:01:42.020
Because high temperature and ambient also, I mean, you should look at.
"""


class TimestampTests(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertAlmostEqual(_timestamp_to_seconds("00:00:00.000"), 0.0)

    def test_minutes(self) -> None:
        self.assertAlmostEqual(_timestamp_to_seconds("00:03:05.500"), 185.5)

    def test_hours(self) -> None:
        self.assertAlmostEqual(_timestamp_to_seconds("01:02:03.040"), 3723.04)

    def test_invalid_returns_zero(self) -> None:
        self.assertEqual(_timestamp_to_seconds("invalid"), 0.0)


class TranscriptParserTests(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(parse_feishu_transcript(""), [])
        self.assertEqual(parse_feishu_transcript("No speakers here."), [])

    def test_parses_segments(self) -> None:
        segments = parse_feishu_transcript(SAMPLE_TRANSCRIPT)
        self.assertEqual(len(segments), 4)
        self.assertEqual(segments[0]["speaker"], "SPEAKER_00")
        self.assertEqual(segments[0]["start"], 0.07)
        self.assertAlmostEqual(segments[0]["end"], 3.08)
        self.assertIn("做APC", segments[0]["text"])
        self.assertEqual(segments[0]["timing_quality"], "feishu_minute")

    def test_speaker_sequence(self) -> None:
        segments = parse_feishu_transcript(SAMPLE_TRANSCRIPT)
        speakers = [s["speaker"] for s in segments]
        self.assertEqual(speakers, ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_01"])

    def test_last_segment_end_defaults(self) -> None:
        segments = parse_feishu_transcript(SAMPLE_TRANSCRIPT)
        # Last segment end defaults to start + 30
        self.assertAlmostEqual(segments[-1]["end"], 102.02 + 30)

    def test_text_trimmed(self) -> None:
        segments = parse_feishu_transcript(SAMPLE_TRANSCRIPT)
        self.assertFalse(segments[1]["text"].startswith("\n"))
        self.assertTrue(segments[1]["text"].endswith("ideas."))

    def test_segment_ids_unique(self) -> None:
        segments = parse_feishu_transcript(SAMPLE_TRANSCRIPT)
        ids = [s["id"] for s in segments]
        self.assertEqual(len(ids), len(set(ids)))


class SpeakerParserTests(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(parse_minute_speakers(""), {})

    def test_extracts_speakers(self) -> None:
        mapping = parse_minute_speakers(SAMPLE_TRANSCRIPT)
        self.assertEqual(len(mapping), 3)
        self.assertIn("SPEAKER_00", mapping)
        self.assertEqual(mapping["SPEAKER_00"]["name"], "Speaker 1")
        self.assertIn("color", mapping["SPEAKER_00"])
        self.assertEqual(mapping["SPEAKER_01"]["name"], "Speaker 2")
        self.assertEqual(mapping["SPEAKER_02"]["name"], "Speaker 3")

    def test_colors_assigned(self) -> None:
        mapping = parse_minute_speakers(SAMPLE_TRANSCRIPT)
        colors = [v["color"] for v in mapping.values()]
        self.assertEqual(len(colors), len(set(colors)))


if __name__ == "__main__":
    unittest.main()
