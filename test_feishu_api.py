"""测试 B流程 API 路由：验证端点存在、错误处理正确。

不实际调用 lark-cli（需要真实音频+额度）；只验证路由和边界条件。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import customers
import app as app_module


class FeishuMinutesRouteTests(unittest.TestCase):
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

    def _make_project(self, project_id: str = "fsm01") -> str:
        folder = app_module.PROJECTS_DIR / project_id
        folder.mkdir(parents=True, exist_ok=True)
        project = {
            "id": project_id,
            "title": "飞书妙记测试",
            "created_at": app_module.now(),
            "updated_at": app_module.now(),
            "customer_id": "",
            "customer_name": "",
            "media_path": "",
            "media_paths": [],
            "audio_path": "",
            "file_manifest": [],
            "diarization_segments": [],
            "asr_segments": [],
            "asr_raw_text": "",
            "transcript_segments": [],
            "speaker_map": {},
        }
        app_module.save_project(project)
        return project_id

    def test_feishu_minutes_no_audio_returns_400(self) -> None:
        pid = self._make_project()
        response = self.client.post(f"/api/projects/{pid}/process/feishu-minutes")
        self.assertEqual(response.status_code, 400)
        self.assertIn("源文件", response.get_json()["error"])

    def test_feishu_minutes_nonexistent_project_400(self) -> None:
        response = self.client.post("/api/projects/nonexistent/process/feishu-minutes")
        self.assertEqual(response.status_code, 400)

    def test_push_report_no_report_returns_400(self) -> None:
        pid = self._make_project()
        response = self.client.post(f"/api/projects/{pid}/push-report-to-feishu", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("报告", response.get_json()["error"])

    def test_push_report_nonexistent_project_400(self) -> None:
        response = self.client.post("/api/projects/nonexistent/push-report-to-feishu", json={})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()

