#!/usr/bin/env python3
"""
review-fix-pipeline 側の共通 review outcome contract builder。

役割:
1. reviewer / path / items を正規化する
2. Claude Code / Codex どちらの runtime でも共通 payload を生成する
3. 必要なら外部 producer（例: claude-review-pdca の record-review-outcome.py）へ渡す
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REVIEWER_ALIASES = {
    "/ifr": "intent-first-review",
    "ifr": "intent-first-review",
    "sc-ifr": "intent-first-review",
    "intent-first-review": "intent-first-review",
    "/rfl": "review-fix-loop",
    "/review-fix-loop": "review-fix-loop",
    "sc-rfl": "review-fix-loop",
    "sc-review-fix-loop": "review-fix-loop",
    "review-fix-loop": "review-fix-loop",
    "sc-gr": "go-robust",
    "/go-robust": "go-robust",
    "go-robust": "go-robust",
    "sc-ir": "intent-review-light",
    "intent-review-light": "intent-review-light",
}

ALLOWED_SEVERITIES = {"critical", "high", "warning", "info", "nitpick"}
ALLOWED_STATUSES = {"pending", "fixed", "judgment-required"}
ALLOWED_TYPES = {"finding", "judgment_call"}
ALLOWED_CONFIDENCE = {"low", "medium", "high"}
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDCA_PRODUCER = (
    PROJECT_ROOT.parent / "claude-review-pdca" / "scripts" / "record-review-outcome.py"
)


def normalize_reviewer(value: str | None) -> str:
    normalized = str(value or "").strip()
    return REVIEWER_ALIASES.get(normalized, normalized or "unknown-reviewer")


def normalize_path(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).replace("\\", "/").rstrip("/")
    return normalized or None


def detect_repo_root(cwd: str = ".") -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return normalize_path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def warn_if_repo_root_points_to_bridge_repo(
    repo_root: str | None,
    *,
    forward_to_pdca: bool = False,
    explicit_repo_root: bool = False,
) -> None:
    """PDCA forward時に review-fix-pipeline 自身を target repo と誤認していそうなら警告する。"""
    if not forward_to_pdca or explicit_repo_root or not repo_root:
        return
    normalized_project_root = normalize_path(str(PROJECT_ROOT))
    if normalize_path(repo_root) == normalized_project_root:
        print(
            "Warning: repo_root が review-fix-pipeline 自身を指しています。"
            " 対象が別repoなら --repo-root か --cwd を明示してください。",
            file=sys.stderr,
        )


def load_items_from_args(args: argparse.Namespace) -> list[dict]:
    if args.items_json:
        try:
            items = json.loads(args.items_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"--items-json のJSONが不正: {e}") from e
    else:
        source = Path(args.items_file)
        if not source.exists():
            raise ValueError(f"items file が見つかりません: {args.items_file}")
        try:
            items = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"items file のJSONが不正: {e}") from e

    if not isinstance(items, list):
        raise ValueError("items は配列である必要があります")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("items の各要素は object である必要があります")
    return items


def normalize_item(item: dict, repo_root: str | None) -> dict:
    item_type = str(item.get("type") or "finding").strip()
    if item_type not in ALLOWED_TYPES:
        item_type = "finding"

    severity = str(item.get("severity") or "info").strip().lower()
    if severity not in ALLOWED_SEVERITIES:
        severity = "info"

    status = str(item.get("status") or "pending").strip().lower()
    if status not in ALLOWED_STATUSES:
        status = "pending"

    confidence = str(item.get("confidence") or "medium").strip().lower()
    if confidence not in ALLOWED_CONFIDENCE:
        confidence = "medium"

    file_path = normalize_path(item.get("file_path"))
    if file_path and repo_root and file_path.startswith(repo_root.rstrip("/") + "/"):
        file_path = file_path[len(repo_root.rstrip("/") + "/") :]

    normalized = {
        "type": item_type,
        "title": str(item.get("title") or "").strip(),
        "summary": str(item.get("summary") or "").strip(),
        "severity": severity,
        "category": str(item.get("category") or "").strip(),
        "file_path": file_path,
        "line": item.get("line"),
        "status": status,
        "auto_fixable": bool(item.get("auto_fixable")),
        "needs_judgment": bool(item.get("needs_judgment")),
        "confidence": confidence,
    }
    return normalized


def build_payload(
    *,
    reviewer: str,
    items: list[dict],
    session_id: str | None = None,
    repo_root: str | None = None,
    runtime: str = "unknown",
    mode: str = "normal",
    target_files: list[str] | None = None,
    verification_commands: list[str] | None = None,
    verification_summary: str | None = None,
) -> dict:
    normalized_reviewer = normalize_reviewer(reviewer)
    normalized_repo_root = normalize_path(repo_root)
    normalized_items = [normalize_item(item, normalized_repo_root) for item in items]
    normalized_target_files = [normalize_path(path) for path in (target_files or [])]
    normalized_target_files = [path for path in normalized_target_files if path]

    payload = {
        "schema_version": 1,
        "session_id": str(session_id or "").strip(),
        "repo_root": normalized_repo_root,
        "reviewer": normalized_reviewer,
        "runtime": str(runtime or "unknown").strip() or "unknown",
        "mode": str(mode or "normal").strip() or "normal",
        "target": {
            "kind": "files",
            "files": normalized_target_files,
        },
        "items": normalized_items,
        "verification": {
            "commands": list(verification_commands or []),
            "summary": str(verification_summary or "").strip(),
        },
    }
    return payload


def resolve_producer_path(
    producer_path: str | None = None,
    *,
    pdca_root: str | None = None,
) -> str | None:
    explicit = str(producer_path or "").strip()
    if explicit:
        return explicit

    env_producer = str(os.environ.get("PDCA_PRODUCER_PATH") or "").strip()
    if env_producer:
        return env_producer

    root_hint = (
        str(pdca_root or "").strip() or str(os.environ.get("CLAUDE_REVIEW_PDCA_ROOT") or "").strip()
    )
    if root_hint:
        candidate = Path(root_hint) / "scripts" / "record-review-outcome.py"
        if candidate.exists():
            return str(candidate)

    if DEFAULT_PDCA_PRODUCER.exists():
        return str(DEFAULT_PDCA_PRODUCER)

    return None


def forward_to_producer(
    producer_path: str,
    payload: dict,
    *,
    cwd: str = ".",
    classify_patterns: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        producer_path,
        "--payload-json",
        json.dumps(payload, ensure_ascii=False),
        "--cwd",
        cwd,
    ]
    if classify_patterns:
        cmd.append("--classify-patterns")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build shared review outcome contract payload")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--items-json", help="review items JSON array")
    group.add_argument("--items-file", help="review items JSON file")
    parser.add_argument("--reviewer", required=True, help="reviewer / skill name")
    parser.add_argument("--session-id", help="session id")
    parser.add_argument("--repo-root", help="repo root (default: git detection)")
    parser.add_argument("--cwd", default=".", help="repo root detection fallback cwd")
    parser.add_argument("--runtime", default="unknown", help="claude-code / codex / unknown")
    parser.add_argument("--mode", default="normal", help="normal / review-only")
    parser.add_argument(
        "--target-file", action="append", default=[], help="target file (repeatable)"
    )
    parser.add_argument(
        "--verification-command", action="append", default=[], help="verification command"
    )
    parser.add_argument("--verification-summary", default="", help="verification summary")
    parser.add_argument("--producer-path", help="optional path to downstream PDCA producer")
    parser.add_argument("--pdca-root", help="path to claude-review-pdca repo root")
    parser.add_argument(
        "--forward-to-pdca",
        action="store_true",
        help="resolve and call downstream PDCA producer automatically",
    )
    parser.add_argument(
        "--classify-patterns",
        action="store_true",
        help="pass --classify-patterns to downstream PDCA producer",
    )
    args = parser.parse_args()

    try:
        items = load_items_from_args(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    repo_root = normalize_path(args.repo_root) or detect_repo_root(args.cwd)
    warn_if_repo_root_points_to_bridge_repo(
        repo_root,
        forward_to_pdca=bool(args.producer_path or args.forward_to_pdca),
        explicit_repo_root=bool(args.repo_root),
    )
    payload = build_payload(
        reviewer=args.reviewer,
        items=items,
        session_id=args.session_id,
        repo_root=repo_root,
        runtime=args.runtime,
        mode=args.mode,
        target_files=args.target_file,
        verification_commands=args.verification_command,
        verification_summary=args.verification_summary,
    )

    producer_path = None
    if args.producer_path or args.forward_to_pdca:
        producer_path = resolve_producer_path(args.producer_path, pdca_root=args.pdca_root)
        if not producer_path:
            print(
                "Error: downstream PDCA producer が見つかりません。"
                " --producer-path / --pdca-root / PDCA_PRODUCER_PATH / CLAUDE_REVIEW_PDCA_ROOT を確認してください。",
                file=sys.stderr,
            )
            return 1

    if producer_path:
        result = forward_to_producer(
            producer_path,
            payload,
            cwd=args.cwd,
            classify_patterns=args.classify_patterns,
        )
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.stdout:
            print(result.stdout, end="")
        return result.returncode

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
