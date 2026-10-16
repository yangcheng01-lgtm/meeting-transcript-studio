from __future__ import annotations

import os
import unittest

os.environ["MEETING_DATA_ROOT"] = "D:\\多听工作台"
os.environ["MEETING_PROJECTS_DIR"] = "D:\\多听工作台\\data\\projects"

from app import llm_report_prompt, load_project


class SmokeReportPromptTests(unittest.TestCase):
    def test_report_prompt_ali_meeting(self) -> None:
        os.environ["MEETING_DATA_ROOT"] = "D:\\多听工作台"
        os.environ["MEETING_PROJECTS_DIR"] = "D:\\多听工作台\\data\\projects"
        # reload project path lazily by setting module-level PROJECTS_DIR
        import app as app_module
        from pathlib import Path
        app_module.PROJECTS_DIR = Path(os.environ["MEETING_PROJECTS_DIR"])
        project = load_project("257bf3c3a6")
        system, user, metadata = llm_report_prompt(project, "meeting-minutes-synthesis-zh")
        self.assertGreater(len(system), 100)
        self.assertGreater(len(user), 100)
        self.assertIn("会议纪要", metadata["title"])
        self.assertIn("行动项", user)


if __name__ == "__main__":
    unittest.main()