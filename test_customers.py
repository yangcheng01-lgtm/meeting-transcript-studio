from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import customers


class CustomerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(customers.os.environ, {"MEETING_DATA_ROOT": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _write(self, payload: dict) -> None:
        path = customers.customers_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(customers.list_customers(), [])

    def test_create_customer_persists(self) -> None:
        created = customers.create_customer("Voi")
        self.assertEqual(created["name"], "Voi")
        self.assertEqual(customers.list_customers()[0]["name"], "Voi")

    def test_create_duplicate_rejected(self) -> None:
        customers.create_customer("Voi")
        with self.assertRaises(ValueError):
            customers.create_customer(" Voi ")

    def test_ensure_customer_by_id_or_new_name(self) -> None:
        created = customers.create_customer("Voi")
        self.assertEqual(customers.ensure_customer(created["id"], "")["id"], created["id"])
        self.assertEqual(customers.ensure_customer("", "Tier")["name"], "Tier")
        with self.assertRaises(ValueError):
            customers.ensure_customer("", "")

    def test_add_person_deduplicates(self) -> None:
        created = customers.create_customer("Voi")
        first = customers.add_person(created["id"], "John", "客户技术负责人")
        second = customers.add_person(created["id"], "John", "")
        self.assertEqual(first["id"], second["id"])
        payload = json.loads(customers.customers_path().read_text(encoding="utf-8"))
        self.assertEqual(len(payload["customers"][0]["people"]), 1)

    def test_default_thread_title(self) -> None:
        title = customers.default_thread_title("Voi", ["a.mp3", "b.mp3"])
        self.assertIn("Voi｜", title)
        self.assertIn("2段录音", title)


if __name__ == "__main__":
    unittest.main()