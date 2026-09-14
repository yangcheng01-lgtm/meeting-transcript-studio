from __future__ import annotations

import unittest

from app import (
    app, available_summary_skills, normalize_glossary, normalize_llm_base_url,
    normalize_diarization, split_transcript_for_llm,
)


class CoreSmokeTests(unittest.TestCase):
    def test_llm_base_url_normalization(self) -> None:
        self.assertEqual(normalize_llm_base_url("https://example.test/"), "https://example.test")
        self.assertEqual(normalize_llm_base_url("https://example.test/v1"), "https://example.test")

    def test_diarization_normalization(self) -> None:
        result = normalize_diarization({"segments": [{"start": 0, "end": 1.5, "speaker": "SPEAKER_00"}]})
        self.assertEqual(result[0]["speaker"], "SPEAKER_00")
        self.assertEqual(result[0]["end"], 1.5)

    def test_glossary_normalization(self) -> None:
        self.assertEqual(normalize_glossary("Ninebot\nC035，Ninebot"), ["Ninebot", "C035"])

    def test_transcript_chunking_preserves_lines(self) -> None:
        chunks = split_transcript_for_llm("a\nbb\nccc", max_chars=4)
        self.assertEqual("\n".join(chunks), "a\nbb\nccc")

    def test_summary_skills_are_discoverable(self) -> None:
        skill_ids = {item["id"] for item in available_summary_skills()}
        self.assertIn("meeting-minutes-synthesis-zh", skill_ids)
        self.assertIn("business-interview-insight-zh", skill_ids)

    def test_summary_skills_api(self) -> None:
        response = app.test_client().get("/api/summary-skills")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.get_json()["skills"]), 4)

    def test_gateway_key_configures_llm_and_asr(self) -> None:
        response = app.test_client().put("/api/llm/config", json={
            "api_url": "https://example.test", "api_key": "test-only", "model": "external/glm-test",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["asr_configured"])


if __name__ == "__main__":
    unittest.main()
