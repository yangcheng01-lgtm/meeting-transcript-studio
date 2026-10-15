from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import customers
import voiceprints
import app as app_module


class VoiceprintApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(customers.os.environ, {"MEETING_DATA_ROOT": self.tmp.name})
        env.start()
        # 重定向 app 的 PROJECTS_DIR 到临时目录
        self.projects_patch = mock.patch.object(app_module, "PROJECTS_DIR", Path(self.tmp.name) / "projects")
        self.projects_patch.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.projects_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.client = app_module.app.test_client()
        self.customer = customers.create_customer("Voi")
        project_id = "vp01"
        folder = app_module.PROJECTS_DIR / project_id
        folder.mkdir(parents=True, exist_ok=True)
        project = {
            "id": project_id, "title": "test",
            "customer_id": self.customer["id"], "customer_name": "Voi",
            "speaker_map": {"SPEAKER_00": {"name": "", "role": ""}},
            "diarization_segments": [{"start": 0, "end": 3, "speaker": "SPEAKER_00"}],
            "audio_path": "", "media_path": "",
        }
        (folder / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
        self.project_id = project_id

    def test_match_requires_customer(self) -> None:
        # 客户档案无声纹时 decision 为 new
        response = self.client.post(f"/api/projects/{self.project_id}/voiceprint-match/SPEAKER_00")
        self.assertEqual(response.status_code, 400)  # 尚未生成代表片段，期待报错

    def test_save_speaker_requires_name(self) -> None:
        response = self.client.post(f"/api/projects/{self.project_id}/voiceprint-save/SPEAKER_00", json={})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()