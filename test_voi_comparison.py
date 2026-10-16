"""对照测试：用 D 盘真实 AliMeeting 项目验证 report prompt 和导出链路。

前置条件：D:\\客户声音\\data\\projects\\257bf3c3a6 存在。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

ALI_PROJECT_ID = "257bf3c3a6"
DATA_ROOT = Path(r"D:\客户声音")
PROJECTS_DIR = DATA_ROOT / "data" / "projects"


@unittest.skipUnless(
    (PROJECTS_DIR / ALI_PROJECT_ID / "project.json").is_file(),
    f"AliMeeting project {ALI_PROJECT_ID} not found on D: drive",
)
class AliMeetingRealProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["MEETING_DATA_ROOT"] = str(DATA_ROOT)
        os.environ["MEETING_PROJECTS_DIR"] = str(PROJECTS_DIR)
        import importlib
        import app as app_module
        importlib.reload(app_module)
        cls.app_module = app_module

    def test_transcript_for_agent_has_speakers(self) -> None:
        project = self.app_module.load_project(ALI_PROJECT_ID)
        transcript = self.app_module.transcript_for_agent(project)
        self.assertGreater(len(transcript), 100)
        # Should contain timestamps
        self.assertIn("[00:00:", transcript)
        # Should contain 4 distinct speaker labels (AliMeeting uses SPEAKER_00..03)
        for label in ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03"):
            self.assertIn(label, transcript)

    def test_report_context_includes_metadata(self) -> None:
        project = self.app_module.load_project(ALI_PROJECT_ID)
        context = self.app_module.report_context(project)
        self.assertIn("AliMeeting", context)
        self.assertIn("文件1", context)
        self.assertIn("00:00:00.00", context)

    def test_report_prompt_has_rules(self) -> None:
        project = self.app_module.load_project(ALI_PROJECT_ID)
        system, user, metadata = self.app_module.llm_report_prompt(
            project, "meeting-minutes-synthesis-zh"
        )
        self.assertIn("Skill 规则", system)
        self.assertIn("行动项", user)
        self.assertIn("保留关键数字", user)

    def test_export_blocks_unnamed_speakers(self) -> None:
        """AliMeeting project has SPEAKER_00 placeholder names; export should reject."""
        project = self.app_module.load_project(ALI_PROJECT_ID)
        with self.assertRaises(ValueError):
            self.app_module.render_exports(project)

    def test_segment_source_single_file(self) -> None:
        project = self.app_module.load_project(ALI_PROJECT_ID)
        manifest = project.get("file_manifest", [])
        self.assertEqual(len(manifest), 1)
        result = self.app_module.segment_source_at(manifest, 30)
        self.assertIsNotNone(result)
        self.assertEqual(result["filename"], "audio_16k_mono_60s.wav")

    def test_summary_skills_discoverable(self) -> None:
        skills = self.app_module.available_summary_skills()
        skill_ids = {s["id"] for s in skills}
        self.assertIn("meeting-minutes-synthesis-zh", skill_ids)
        self.assertGreaterEqual(len(skills), 4)


if __name__ == "__main__":
    unittest.main()

