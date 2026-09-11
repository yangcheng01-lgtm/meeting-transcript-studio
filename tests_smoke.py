from __future__ import annotations

import unittest

from app import normalize_llm_base_url, normalize_diarization


class CoreSmokeTests(unittest.TestCase):
    def test_llm_base_url_normalization(self) -> None:
        self.assertEqual(normalize_llm_base_url("https://example.test/"), "https://example.test")
        self.assertEqual(normalize_llm_base_url("https://example.test/v1"), "https://example.test")

    def test_diarization_normalization(self) -> None:
        result = normalize_diarization({"segments": [{"start": 0, "end": 1.5, "speaker": "SPEAKER_00"}]})
        self.assertEqual(result[0]["speaker"], "SPEAKER_00")
        self.assertEqual(result[0]["end"], 1.5)


if __name__ == "__main__":
    unittest.main()
