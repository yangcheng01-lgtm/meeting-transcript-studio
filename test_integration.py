"""MVP 主流程集成测试：客户档案 → 多文件项目 → 说话人命名 → 导出 → 声纹回写 → 纪要 prompt。

不依赖 pyannote 或 ASR；直接构造项目 JSON 验证核心函数链路。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import customers
import voiceprints
import app as app_module


class IntegrationFlowTests(unittest.TestCase):
    """MVP acceptance: create Thread with 2 files → merge timeline → name speakers → export → save to customer."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(customers.os.environ, {"MEETING_DATA_ROOT": self.tmp.name})
        env.start()
        self.projects_patch = mock.patch.object(app_module, "PROJECTS_DIR", Path(self.tmp.name) / "projects")
        self.projects_patch.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.projects_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.client = app_module.app.test_client()

    def _make_project(self, customer_id: str, customer_name: str) -> dict:
        """Create a 2-file project with diarization + transcript, simulating the merged timeline."""
        project_id = "itg001"
        folder = app_module.PROJECTS_DIR / project_id
        folder.mkdir(parents=True, exist_ok=True)
        manifest = [
            {"index": 1, "filename": "a.wav", "start": 0.0, "end": 10.0, "duration": 10.0},
            {"index": 2, "filename": "b.wav", "start": 10.0, "end": 25.0, "duration": 15.0},
        ]
        project = {
            "id": project_id,
            "title": f"{customer_name}｜20261016｜2段录音会议",
            "created_at": app_module.now(),
            "updated_at": app_module.now(),
            "customer_id": customer_id,
            "customer_name": customer_name,
            "media_path": str(folder / "a.wav"),
            "media_paths": [str(folder / "a.wav"), str(folder / "b.wav")],
            "source_count": 2,
            "audio_path": str(folder / "audio_16k_mono.wav"),
            "file_manifest": manifest,
            "diarization_segments": [
                {"start": 0, "end": 5, "speaker": "SPEAKER_00"},
                {"start": 5, "end": 8, "speaker": "SPEAKER_01"},
                {"start": 10, "end": 15, "speaker": "SPEAKER_00"},
                {"start": 15, "end": 22, "speaker": "SPEAKER_01"},
            ],
            "asr_segments": [],
            "asr_raw_text": "",
            "transcript_segments": [
                {"id": "s1", "start": 0.0, "end": 5.0, "speaker": "SPEAKER_00", "text": "第一段开始。", "timing_quality": "speaker_block"},
                {"id": "s2", "start": 5.0, "end": 8.0, "speaker": "SPEAKER_01", "text": "收到。", "timing_quality": "speaker_block"},
                {"id": "s3", "start": 10.0, "end": 15.0, "speaker": "SPEAKER_00", "text": "第二段文件继续。", "timing_quality": "speaker_block"},
                {"id": "s4", "start": 15.0, "end": 22.0, "speaker": "SPEAKER_01", "text": "明白。", "timing_quality": "speaker_block"},
            ],
            "speaker_map": {
                "SPEAKER_00": {"name": "", "role": "", "color": "#e05a33"},
                "SPEAKER_01": {"name": "", "role": "", "color": "#1b9aaa"},
            },
            "expected_speakers": 2,
            "glossary": ["Ninebot", "Voi"],
        }
        app_module.save_project(project)
        return project

    def test_full_flow(self) -> None:
        # 1. Create customer
        customer = customers.create_customer("Voi")
        cid = customer["id"]

        # 2. Create 2-file project
        project = self._make_project(cid, "Voi")
        pid = project["id"]

        # 3. Name speakers via API
        loaded = app_module.load_project(pid)
        loaded["speaker_map"]["SPEAKER_00"]["name"] = "Alice"
        loaded["speaker_map"]["SPEAKER_00"]["role"] = "PM"
        loaded["speaker_map"]["SPEAKER_01"]["name"] = "Bob"
        loaded["speaker_map"]["SPEAKER_01"]["role"] = "工程"
        app_module.save_project(loaded)

        # 4. Export transcript (MD/TXT/SRT)
        paths = app_module.render_exports(loaded)
        md = Path(paths["markdown"]).read_text(encoding="utf-8")
        txt = Path(paths["text"]).read_text(encoding="utf-8")
        srt = Path(paths["srt"]).read_text(encoding="utf-8")
        self.assertIn("Alice", md)
        self.assertIn("Bob", md)
        self.assertIn("文件1：a.wav", md)
        self.assertIn("文件2：b.wav", md)
        self.assertIn("[00:00:00.00", md)
        self.assertIn("[00:00:10.00", md)
        self.assertIn("第二段文件继续", md)
        self.assertIn("Alice", txt)
        self.assertIn("Bob", txt)
        self.assertIn("-->", srt)
        self.assertIn("Alice", srt)
        self.assertIn("Bob", srt)

        # 5. Save speakers to customer archive
        response = self.client.post(f"/api/projects/{pid}/save-speakers-to-customer")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sorted(response.get_json()["saved"]), ["Alice", "Bob"])

        payload = customers.read_customers()
        people = payload["customers"][0]["people"]
        names = {p["name"] for p in people}
        self.assertIn("Alice", names)
        self.assertIn("Bob", names)

        # 6. Report prompt uses merged transcript as sole input
        system, user, metadata = app_module.llm_report_prompt(loaded, "meeting-minutes-synthesis-zh")
        self.assertIn("行动项", user)
        self.assertIn("Skill 规则", system)
        agent_transcript = app_module.transcript_for_agent(loaded)
        self.assertIn("第二段文件继续", agent_transcript)
        self.assertIn("Alice", agent_transcript)
        self.assertIn("Bob", agent_transcript)

    def test_unnamed_speakers_block_export(self) -> None:
        customer = customers.create_customer("Tier")
        project = self._make_project(customer["id"], "Tier")
        with self.assertRaises(ValueError):
            app_module.render_exports(project)

    def test_segment_source_across_files(self) -> None:
        customer = customers.create_customer("Tier")
        project = self._make_project(customer["id"], "Tier")
        manifest = project["file_manifest"]
        self.assertEqual(app_module.segment_source_at(manifest, 3)["filename"], "a.wav")
        self.assertEqual(app_module.segment_source_at(manifest, 12)["filename"], "b.wav")
        self.assertEqual(app_module.segment_source_at(manifest, 12)["relative_start"], 2.0)

    def test_voiceprint_round_trip(self) -> None:
        """After naming speakers, save a voiceprint; then match returns auto."""
        customer = customers.create_customer("Voi")
        cid = customer["id"]
        # Save speaker with voiceprint
        payload = customers.read_customers()
        voiceprints.ensure_person_with_voiceprint(payload, cid, "Alice", "PM", [1.0, 0.0, 0.5])
        customers.save_customers(payload)
        # Match identical vector
        people = customers.read_customers()["customers"][0]["people"]
        result = voiceprints.match_speaker([1.0, 0.0, 0.5], people)
        self.assertEqual(result["decision"], "auto")
        self.assertEqual(result["name"], "Alice")
        # Match orthogonal vector
        result2 = voiceprints.match_speaker([0.0, 1.0, 0.0], people)
        self.assertEqual(result2["decision"], "new")

    def test_voiceprint_api_save_and_match(self) -> None:
        customer = customers.create_customer("Voi")
        project = self._make_project(customer["id"], "Voi")
        project["speaker_map"]["SPEAKER_00"]["name"] = "Alice"
        project["diarization_segments"].append({"start": 0, "end": 3, "speaker": "SPEAKER_00"})
        app_module.save_project(project)
        pid = project["id"]
        # Save voiceprint via API requires real audio clip + pyannote venv;
        # in unit test we verify the guard: without a clip it returns 400.
        response = self.client.post(
            f"/api/projects/{pid}/voiceprint-save/SPEAKER_00",
            json={"name": "Alice", "role": "PM"},
        )
        self.assertEqual(response.status_code, 400)
        # Direct voiceprint write via voiceprints module works without audio.
        payload = customers.read_customers()
        voiceprints.ensure_person_with_voiceprint(payload, str(customer["id"]), "Alice", "PM", [1.0, 0.0])
        customers.save_customers(payload)
        people = customers.read_customers()["customers"][0]["people"]
        alice = next(p for p in people if p["name"] == "Alice")
        self.assertEqual(len(alice["voiceprints"]), 1)


if __name__ == "__main__":
    unittest.main()

