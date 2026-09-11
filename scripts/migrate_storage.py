"""Migrate local 多听工作台 data and source materials to D:.

This script moves only the known runtime/source directories, rewrites absolute
paths inside project.json, and writes a local manifest outside the Git repo.
Run it while the studio server is stopped.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]
DOCUMENTS_ROOT = APP_DIR.parent
USER_HOME = Path.home()
DEFAULT_DATA_ROOT = Path(r"D:\多听工作台")


def path_key(path: Path) -> str:
    return str(path.resolve()).rstrip("\\/").lower()


def replace_prefix(value: str, mappings: list[tuple[Path, Path]]) -> str:
    result = value
    for old, new in mappings:
        old_text = path_key(old)
        current = result.replace("/", "\\")
        current_key = current.lower()
        if current_key == old_text or current_key.startswith(old_text + "\\"):
            suffix = current[len(str(old).rstrip("\\/")) :]
            result = str(new).rstrip("\\/") + suffix
    return result


def rewrite_paths(value: Any, mappings: list[tuple[Path, Path]]) -> Any:
    if isinstance(value, str):
        return replace_prefix(value, mappings)
    if isinstance(value, list):
        return [rewrite_paths(item, mappings) for item in value]
    if isinstance(value, dict):
        return {key: rewrite_paths(item, mappings) for key, item in value.items()}
    return value


def rewrite_project_json(projects_dir: Path, mappings: list[tuple[Path, Path]], dry_run: bool) -> int:
    changed = 0
    for path in projects_dir.glob("*/project.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rewritten = rewrite_paths(payload, mappings)
        if rewritten != payload:
            changed += 1
            if not dry_run:
                path.write_text(json.dumps(rewritten, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def safe_move(source: Path, target: Path, data_root: Path, dry_run: bool) -> None:
    source = source.resolve()
    target = target.resolve()
    root_key = path_key(data_root)
    target_key = path_key(target)
    if not (target_key == root_key or target_key.startswith(root_key + "\\")):
        raise RuntimeError(f"Refusing target outside data root: {target}")
    if not source.exists():
        return
    if target.exists():
        raise RuntimeError(f"Target already exists; refusing to merge automatically: {target}")
    print(f"MOVE {source} -> {target}")
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_root = Path(args.data_root).expanduser().resolve()
    projects_source = APP_DIR / "data" / "projects"
    projects_target = data_root / "data" / "projects"
    source_materials = data_root / "source-materials"

    moves: list[tuple[Path, Path]] = [
        (projects_source, projects_target),
        (DOCUMENTS_ROOT / "2026-07-30_Gbike会议", source_materials / "2026-07-30_Gbike会议"),
        (DOCUMENTS_ROOT / "会话总结_0826", source_materials / "会话总结_0826"),
        (DOCUMENTS_ROOT / "源文件", source_materials / "源文件"),
        (DOCUMENTS_ROOT / "公开测试数据", source_materials / "公开测试数据"),
        (DOCUMENTS_ROOT / "测试案例", source_materials / "测试案例"),
        (DOCUMENTS_ROOT / "mvp_韩国拜访_前3分钟.wav", source_materials / "mvp_韩国拜访_前3分钟.wav"),
        (APP_DIR / "mvp_韩国拜访_40s片段.wav", source_materials / "mvp_韩国拜访_40s片段.wav"),
        (APP_DIR / "mvp_韩国拜访_前3分钟.wav", source_materials / "mvp_韩国拜访_前3分钟.wav"),
        (USER_HOME / ".cache" / "huggingface", data_root / "model-cache" / "huggingface"),
        (USER_HOME / ".cache" / "torch", data_root / "model-cache" / "torch"),
    ]

    if not args.dry_run:
        data_root.mkdir(parents=True, exist_ok=True)
    for source, target in moves:
        safe_move(source, target, data_root, args.dry_run)

    mappings = [(source, target) for source, target in moves]
    changed = rewrite_project_json(projects_target, mappings, args.dry_run)
    print(f"PROJECT_JSON_REWRITTEN {changed}")

    if not args.dry_run:
        manifest = {
            "migrated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "data_root": str(data_root),
            "projects_dir": str(projects_target),
            "mappings": [{"from": str(source), "to": str(target)} for source, target in mappings],
        }
        (data_root / "migration_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"MANIFEST {data_root / 'migration_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
