"""客户档案存储与检索。

MVP 先用单个 JSON 文件保存客户和人物；声纹向量等下一轮接入。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def customers_path() -> Path:
    data_root = Path(os.environ.get("MEETING_DATA_ROOT", ".")).expanduser()
    path = data_root / "data" / "customers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_customers() -> dict[str, Any]:
    path = customers_path()
    if not path.is_file():
        return {"customers": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("customers"), list):
        return {"customers": []}
    return payload


def save_customers(payload: dict[str, Any]) -> None:
    path = customers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_customer_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def list_customers() -> list[dict[str, Any]]:
    payload = read_customers()
    return sorted(payload["customers"], key=lambda item: item.get("name", ""))


def find_customer(customer_id: str) -> dict[str, Any] | None:
    payload = read_customers()
    return next((item for item in payload["customers"] if item.get("id") == customer_id), None)


def create_customer(name: Any) -> dict[str, Any]:
    clean = normalize_customer_name(name)
    if not clean:
        raise ValueError("客户名称不能为空。")
    payload = read_customers()
    if any(item.get("name") == clean for item in payload["customers"]):
        raise ValueError(f"客户已存在：{clean}")
    customer = {
        "id": uuid.uuid4().hex[:10],
        "name": clean[:80],
        "created_at": _now(),
        "updated_at": _now(),
        "people": [],
    }
    payload["customers"].append(customer)
    save_customers(payload)
    return customer


def ensure_customer(customer_id: Any, new_name: Any) -> dict[str, Any]:
    """解析导入时的客户：优先已有客户 ID，其次新建，二者都缺则报错。"""
    if customer_id:
        customer = find_customer(str(customer_id))
        if not customer:
            raise ValueError("所选客户不存在，请刷新后重试。")
        return customer
    clean = normalize_customer_name(new_name)
    if clean:
        return create_customer(clean)
    raise ValueError("请选择已有客户，或填写新客户名称。")


def add_person(customer_id: str, name: str, role: str = "") -> dict[str, Any] | None:
    """向客户档案添加人物；同名去重。"""
    clean_name = normalize_customer_name(name)
    if not clean_name:
        return None
    payload = read_customers()
    customer = next((item for item in payload["customers"] if item.get("id") == customer_id), None)
    if not customer:
        return None
    person = next((item for item in customer["people"] if item.get("name") == clean_name), None)
    if person:
        if role and not person.get("role"):
            person["role"] = normalize_customer_name(role)
        customer["updated_at"] = _now()
        save_customers(payload)
        return person
    person = {"id": uuid.uuid4().hex[:10], "name": clean_name, "role": normalize_customer_name(role), "voiceprints": []}
    customer["people"].append(person)
    customer["updated_at"] = _now()
    save_customers(payload)
    return person


def default_thread_title(customer_name: str, source_names: list[str]) -> str:
    date = datetime.now().strftime("%Y%m%d")
    if len(source_names) == 1:
        return f"{customer_name}｜{date}｜{Path(source_names[0]).stem}"[:120]
    return f"{customer_name}｜{date}｜{len(source_names)}段录音会议"[:120]