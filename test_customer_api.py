from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import customers
from app import app, PROJECTS_DIR


class CustomerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(customers.os.environ, {"MEETING_DATA_ROOT": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.client = app.test_client()

    def test_list_and_create_customer_api(self) -> None:
        self.assertEqual(self.client.get("/api/customers").get_json(), [])
        response = self.client.post("/api/customers", json={"name": "Voi"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["name"], "Voi")
        self.assertEqual(self.client.get("/api/customers").get_json()[0]["name"], "Voi")

    def test_create_customer_api_rejects_duplicate(self) -> None:
        self.client.post("/api/customers", json={"name": "Voi"})
        response = self.client.post("/api/customers", json={"name": "Voi"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("已存在", response.get_json()["error"])

    def test_save_speakers_requires_customer(self) -> None:
        # 创建一个空项目文件来测试 API 依赖
        project_id = "test123"
        folder = PROJECTS_DIR / project_id
        folder.mkdir(parents=True, exist_ok=True)
        project = {"id": project_id, "title": "test", "customer_id": "", "speaker_map": {"SPEAKER_00": {"name": "John", "role": ""}}}
        (folder / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
        response = self.client.post(f"/api/projects/{project_id}/save-speakers-to-customer")
        self.assertEqual(response.status_code, 400)

    def test_save_speakers_writes_to_customer(self) -> None:
        customer = customers.create_customer("Voi")
        project_id = "test456"
        folder = PROJECTS_DIR / project_id
        folder.mkdir(parents=True, exist_ok=True)
        project = {"id": project_id, "title": "test", "customer_id": customer["id"], "speaker_map": {"SPEAKER_00": {"name": "John", "role": "tech"}, "SPEAKER_01": {"name": "SPEAKER_01", "role": ""}}}
        (folder / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
        response = self.client.post(f"/api/projects/{project_id}/save-speakers-to-customer")
        print(response.get_json()); self.assertEqual(response.status_code, 200); self.assertEqual(response.get_json()["saved"], ["John"])
        payload = json.loads(customers.customers_path().read_text(encoding="utf-8"))
        self.assertEqual(len(payload["customers"][0]["people"]), 1)
        self.assertEqual(payload["customers"][0]["people"][0]["name"], "John")
        self.assertEqual(payload["customers"][0]["people"][0]["role"], "tech")


if __name__ == "__main__":
    unittest.main()