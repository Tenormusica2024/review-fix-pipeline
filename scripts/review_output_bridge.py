#!/usr/bin/env python3
"""
review markdown / machine block を共通 contract payload に変換し、
必要に応じて PDCA producer へ forward する bridge。

想定用途:
- 既存 /ifr /rfl の markdown 出力を壊さず後段 PDCA に接続する
- 将来的に machine-readable block が埋め込まれたら優先利用する
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_outcome_contract import (  # type: ignore
    build_payload,
    detect_repo_root,
    forward_to_producer,
    normalize_path,
    resolve_producer_path,
    warn_if_repo_root_points_to_bridge_repo,
)

FENCE_RE = re.compile(
    r"```(?:review-outcome-json|review-outcome|json)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
SECTION_RE = re.compile(
    r"^##\s*(?P<section>自動修正可|Auto-fixable|要確認|Requires confirmation)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
TITLE_RE = re.compile(r"^###\s*(?P<title>.+?)\s*$", re.MULTILINE)
FIELD_RE = re.compile(r"^\s*(?:-\s*)?(?P<key>severity|auto_fixable|問題|Issue|詳細|Detail|判断ポイント|Decision point|何が起きるか|What happens|対象|Target)\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE)
SEPARATOR_RE = re.compile(r"^\s*─{5,}\s*$", re.MULTILINE)


def load_text(input_text: str | None, input_file: str | None) -> str:
    if input_text is not None:
        return input_text
    if not input_file:
        raise ValueError("input text or file is required")
    path = Path(input_file)
    if not path.exists():
        raise ValueError(f"input file が見つかりません: {input_file}")
    return path.read_text(encoding="utf-8")


def _normalize_severity(value: str | None) -> str:
    severity = str(value or "info").strip().lower()
    return severity if severity in {"critical", "high", "warning", "info", "nitpick"} else "info"


def _split_path_line(value: str | None) -> tuple[str | None, int | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, None
    match = re.match(r"^(?P<path>.+?):(?P<line>\d+)$", raw)
    if match:
        return normalize_path(match.group("path")), int(match.group("line"))
    return normalize_path(raw), None


def extract_machine_block(markdown: str) -> list[dict] | None:
    for match in FENCE_RE.finditer(markdown):
        body = match.group(1).strip()
        if not body:
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            return [item for item in parsed["items"] if isinstance(item, dict)]
        if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
            return parsed
    return None


def _parse_auto_fixable_block(block: str, default_status: str) -> list[dict]:
    items: list[dict] = []
    titles = list(TITLE_RE.finditer(block))
    for index, title_match in enumerate(titles):
        start = title_match.end()
        end = titles[index + 1].start() if index + 1 < len(titles) else len(block)
        body = block[start:end]
        fields = {m.group("key"): m.group("value").strip() for m in FIELD_RE.finditer(body)}
        file_path, line = _split_path_line(fields.get("対象") or fields.get("Target"))
        summary = fields.get("何が起きるか") or fields.get("What happens") or title_match.group("title").strip()
        items.append(
            {
                "type": "finding",
                "title": title_match.group("title").strip(),
                "summary": summary,
                "severity": _normalize_severity(fields.get("severity")),
                "category": "",
                "file_path": file_path,
                "line": line,
                "status": default_status,
                "auto_fixable": True,
                "needs_judgment": False,
                "confidence": "high",
            }
        )
    return items


def _parse_judgment_block(block: str) -> list[dict]:
    items: list[dict] = []
    chunks = [chunk.strip() for chunk in SEPARATOR_RE.split(block) if chunk.strip()]
    for chunk in chunks:
        fields = {m.group("key"): m.group("value").strip() for m in FIELD_RE.finditer(chunk)}
        title = fields.get("問題") or fields.get("Issue") or "judgment required"
        summary = fields.get("詳細") or fields.get("Detail") or title
        file_path, line = _split_path_line(fields.get("対象") or fields.get("Target"))
        items.append(
            {
                "type": "judgment_call" if fields.get("判断ポイント") or fields.get("Decision point") else "finding",
                "title": title,
                "summary": summary,
                "severity": _normalize_severity(fields.get("severity")),
                "category": "",
                "file_path": file_path,
                "line": line,
                "status": "judgment-required",
                "auto_fixable": False,
                "needs_judgment": True,
                "confidence": "medium",
            }
        )
    return items


def parse_legacy_markdown(markdown: str, *, auto_fix_status: str = "pending") -> list[dict]:
    items: list[dict] = []
    sections = list(SECTION_RE.finditer(markdown))
    for index, section_match in enumerate(sections):
        section_name = section_match.group("section").lower()
        start = section_match.end()
        end = sections[index + 1].start() if index + 1 < len(sections) else len(markdown)
        body = markdown[start:end]
        if section_name in {"自動修正可", "auto-fixable"}:
            items.extend(_parse_auto_fixable_block(body, auto_fix_status))
        elif section_name in {"要確認", "requires confirmation"}:
            items.extend(_parse_judgment_block(body))
    return items


def parse_review_output(markdown: str, *, auto_fix_status: str = "pending") -> list[dict]:
    machine_items = extract_machine_block(markdown)
    if machine_items is not None:
        return machine_items
    return parse_legacy_markdown(markdown, auto_fix_status=auto_fix_status)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge review markdown output to shared outcome contract / PDCA producer")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-text", help="review markdown text")
    source.add_argument("--input-file", help="review markdown file")
    parser.add_argument("--reviewer", required=True, help="reviewer / skill name")
    parser.add_argument("--session-id", help="session id")
    parser.add_argument("--repo-root", help="repo root")
    parser.add_argument("--cwd", default=".", help="repo root detection fallback cwd")
    parser.add_argument("--runtime", default="unknown", help="claude-code / codex / unknown")
    parser.add_argument("--mode", default="normal", help="normal / review-only")
    parser.add_argument("--target-file", action="append", default=[], help="target file")
    parser.add_argument("--verification-command", action="append", default=[], help="verification command")
    parser.add_argument("--verification-summary", default="", help="verification summary")
    parser.add_argument("--producer-path", help="optional explicit PDCA producer path")
    parser.add_argument("--pdca-root", help="path to claude-review-pdca root")
    parser.add_argument("--forward-to-pdca", action="store_true", help="forward to downstream PDCA producer")
    parser.add_argument("--classify-patterns", action="store_true", help="pass --classify-patterns to downstream PDCA producer")
    parser.add_argument(
        "--auto-fix-status",
        choices=["pending", "fixed"],
        default="pending",
        help="status assigned to legacy '自動修正可/Auto-fixable' items",
    )
    args = parser.parse_args()

    try:
        markdown = load_text(args.input_text, args.input_file)
        items = parse_review_output(markdown, auto_fix_status=args.auto_fix_status)
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
