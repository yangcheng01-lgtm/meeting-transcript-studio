"""声纹相似度计算与阈值匹配。

向量来自 pyannote/wespeaker-voxceleb-resnet34-LM，维度 256。
阈值：>=0.80 自动确认；0.65-0.80 人工确认；<0.65 新建占位。
"""
from __future__ import annotations

import math
from typing import Any

AUTO_MATCH_THRESHOLD = 0.80
REVIEW_MATCH_THRESHOLD = 0.65


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def match_speaker(
    vector: list[float],
    people: list[dict[str, Any]],
) -> dict[str, Any]:
    """返回该说话人的最佳候选：name、similarity、decision。"""
    best_name = ""
    best_person_id = ""
    best_role = ""
    best_score = -1.0
    for person in people:
        person_best = -1.0
        for record in person.get("voiceprints", []):
            stored = record.get("vector") or []
            score = cosine_similarity(vector, stored)
            if score > person_best:
                person_best = score
        if person_best > best_score:
            best_score = person_best
            best_name = person.get("name", "")
            best_role = person.get("role", "")
            best_person_id = person.get("id", "")
    if best_score < 0:
        return {"decision": "new", "name": "", "role": "", "person_id": "", "similarity": 0.0}
    if best_score >= AUTO_MATCH_THRESHOLD:
        decision = "auto"
    elif best_score >= REVIEW_MATCH_THRESHOLD:
        decision = "review"
    else:
        decision = "new"
    return {"decision": decision, "name": best_name, "role": best_role, "person_id": best_person_id, "similarity": round(best_score, 4)}


def add_voiceprint(customer_payload: dict[str, Any], customer_id: str, person_id: str, vector: list[float], model: str = "pyannote/wespeaker-voxceleb-resnet34-LM") -> bool:
    """向指定人物追加一条声纹向量；最多保留 10 条。"""
    for customer in customer_payload.get("customers", []):
        if customer.get("id") != customer_id:
            continue
        for person in customer.get("people", []):
            if person.get("id") != person_id:
                continue
            records = person.setdefault("voiceprints", [])
            records.append({"vector": vector, "model": model, "saved_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")})
            if len(records) > 10:
                person["voiceprints"] = records[-10:]
            return True
    return False


def ensure_person_with_voiceprint(customer_payload: dict[str, Any], customer_id: str, name: str, role: str, vector: list[float]) -> str:
    """按名字找到或新建人物，并写入声纹；返回 person_id。"""
    for customer in customer_payload.get("customers", []):
        if customer.get("id") != customer_id:
            continue
        for person in customer.get("people", []):
            if person.get("name") == name:
                add_voiceprint(customer_payload, customer_id, person["id"], vector)
                return person["id"]
        new_person = {"id": __import__("uuid").uuid4().hex[:10], "name": name, "role": role, "voiceprints": []}
        customer["people"].append(new_person)
        add_voiceprint(customer_payload, customer_id, new_person["id"], vector)
        return new_person["id"]
    return ""