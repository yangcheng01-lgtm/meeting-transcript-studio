from __future__ import annotations

import os
import unittest
from pathlib import Path

from app import render_exports


class ExportHeaderTests(unittest.TestCase):
    def test_md_header_has_customer_and_manifest(self) -> None:
        os.environ["MEETING_DATA_ROOT"] = "D:\\客户声音"
        os.environ["MEETING_PROJECTS_DIR"] = "D:\\客户声音\\data\\projects"
        import app as app_module
        app_module.PROJECTS_DIR = Path(os.environ["MEETING_PROJECTS_DIR"])
        project = {
            "id": "test-export-01",
            "title": "导出测试",
            "customer_name": "Voi",
            "speaker_map": {"SPEAKER_00": {"name": "John", "role": "", "color": "#e05a33"}},
            "transcript_segments": [{"start": 0.0, "end": 1.5, "speaker": "SPEAKER_00", "text": "你好", "timing_quality": "segment"}],
            "file_manifest": [
                {"index": 1, "filename": "a.wav", "start": 0, "end": 10, "duration": 10},
                {"index": 2, "filename": "b.wav", "start": 10, "end": 25, "duration": 15},
            ],
        }
        app_module.project_dir(project["id"]).mkdir(parents=True, exist_ok=True)
        paths = render_exports(project)
        md = Path(paths["markdown"]).read_text(encoding="utf-8")
        self.assertIn("客户：Voi", md)
        self.assertIn("文件1：a.wav", md)
        self.assertIn("文件2：b.wav", md)
        self.assertIn("John", md)
        self.assertIn("[00:00:00.00", md)


if __name__ == "__main__":
    unittest.main()