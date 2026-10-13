from __future__ import annotations

import unittest

from app import segment_source_at


class SegmentSourceTests(unittest.TestCase):
    def test_single_file(self) -> None:
        manifest = [{"index": 1, "filename": "a.wav", "start": 0, "end": 10, "duration": 10}]
        self.assertEqual(segment_source_at(manifest, 5)["filename"], "a.wav")

    def test_multi_file(self) -> None:
        manifest = [
            {"index": 1, "filename": "a.wav", "start": 0, "end": 10, "duration": 10},
            {"index": 2, "filename": "b.wav", "start": 10, "end": 25, "duration": 15},
        ]
        self.assertEqual(segment_source_at(manifest, 0)["filename"], "a.wav")
        self.assertEqual(segment_source_at(manifest, 9.9)["filename"], "a.wav")
        self.assertEqual(segment_source_at(manifest, 10)["filename"], "b.wav")
        self.assertEqual(segment_source_at(manifest, 24.9)["filename"], "b.wav")

    def test_out_of_range(self) -> None:
        manifest = [{"index": 1, "filename": "a.wav", "start": 0, "end": 10, "duration": 10}]
        self.assertIsNone(segment_source_at(manifest, 15))
        self.assertIsNone(segment_source_at([], 5))

    def test_relative_offset(self) -> None:
        manifest = [
            {"index": 1, "filename": "a.wav", "start": 0, "end": 10, "duration": 10},
            {"index": 2, "filename": "b.wav", "start": 10, "end": 25, "duration": 15},
        ]
        self.assertEqual(segment_source_at(manifest, 20)["relative_start"], 10)


if __name__ == "__main__":
    unittest.main()