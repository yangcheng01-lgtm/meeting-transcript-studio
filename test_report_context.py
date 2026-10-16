from __future__ import annotations

import unittest

from app import app, llm_report_prompt, report_context, resolve_summary_skill


class _FakeProject(dict):
    pass


class ReportContextTests(unittest.TestCase):
    def test_report_context_includes_file_manifest(self) -> None:
        project = {
            "title": "多文件会议",
            "id": "abc123",
            "media_paths": ["/tmp/a.wav", "/tmp/b.wav"],
            "file_manifest": [
                {"index": 1, "filename": "a.wav", "start": 0, "end": 10, "duration": 10},
                {"index": 2, "filename": "b.wav", "start": 10, "end": 25, "duration": 15},
            ],
            "customer_name": "Voi",
            "glossary": [],
        }
        context = report_context(project)
        self.assertIn("a.wav", context)
        self.assertIn("b.wav", context)
        self.assertIn("Voi", context)
        self.assertIn("00:00:00.00", context)
        self.assertIn("00:00:10.00", context)

    def test_report_prompt_has_unified_rules(self) -> None:
        project = {"title": "测试", "id": "x", "media_paths": [], "transcript_segments": [], "glossary": []}
        system, user, metadata = llm_report_prompt(project, "meeting-minutes-synthesis-zh", source_text="A: 你好")
        self.assertIn("Skill 规则", system)
        self.assertIn("保留关键数字", user)
        self.assertIn("行动项", user)


if __name__ == "__main__":
    unittest.main()