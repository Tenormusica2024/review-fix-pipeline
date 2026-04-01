#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_parallel_reviews.py - 並列レビュー結果のマージスクリプト（2〜Nモデル対応）

複数モデルの Step 4 フォーマット Markdown 出力を受け取り、
重複排除・detected_by 付与・severity/auto_fixable 競合解決を行って
統合結果を JSON または Markdown で出力する。

Usage:
    python merge_parallel_reviews.py --opus FILE --glm FILE --codex FILE [--format markdown] [-o OUTPUT]
    python merge_parallel_reviews.py --input opus:FILE --input codex:FILE [--format markdown] [-o OUTPUT]
    python merge_parallel_reviews.py --opus FILE --glm FILE [-o OUTPUT]  # 2モデルでも可

出力: JSON 配列（デフォルト）または Markdown（--format markdown 指定時）
"""

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path
from difflib import SequenceMatcher

# Windows UTF-8 対応
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONUTF8", "1")


# --- severity の順序定義 ---
SEVERITY_ORDER = {"critical": 3, "warning": 2, "info": 1}


def parse_severity(text: str) -> str:
    """テキストから severity を抽出。'severity:' フィールドを最優先し、
    見つからなければ自由文から推定。どちらもなければ 'info' を返す"""
    # まず 'severity: xxx' フィールドを厳密に探す（自由文より優先）
    field_match = re.search(r'severity\s*[:：]\s*(critical|warning|info)', text, re.IGNORECASE)
    if field_match:
        return field_match.group(1).lower()
    # フィールドがない場合のみ、自由文から推定（[critical] 等のラベル形式を優先）
    label_match = re.search(r'\[(critical|warning|info)\]', text, re.IGNORECASE)
    if label_match:
        return label_match.group(1).lower()
    return "info"


def parse_file_path_line(text: str) -> tuple[str, int | None]:
    """テキストからファイルパス:行番号を抽出"""
    # パターン: filepath:line or filepath:行番号
    # Windows パス (C:\...) と Unix パス (/...) の両方に対応
    patterns = [
        r'対象\s*[:：]\s*`?\[?([^\]`\n]+?)\]?:(\d+)`?',  # 対象: [path:line] or 対象: path:line
        r'@\s*`?\[?([^\]`\n]+?)\]?:(\d+)`?',       # @ [path:line] or @ path:line
        r'`([^`]+?):(\d+)`',                         # `path:line`
        r'([\w/\\._-]+\.\w+):(\d+)',                 # path.ext:line（拡張子必須）
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            # 角括弧やバッククォートの残留を除去
            path = match.group(1).strip().strip("[]`")
            return path, int(match.group(2))
    return "", None


def extract_markdown_review_block(raw_output: str) -> str:
    """生の出力からレビュー本文ブロックを抽出（前置き・後書き除去）"""
    raw_output = raw_output.lstrip("\ufeff")
    # '意図:' または '## 自動修正可' で始まるブロックを探す
    markers = [r'^意図:', r'^## 自動修正可', r'^## レビュー結果']
    for marker in markers:
        match = re.search(marker, raw_output, re.MULTILINE)
        if match:
            return raw_output[match.start():]
    # マーカーが見つからない場合は全文を返す
    return raw_output


def has_structured_review_markers(text: str) -> bool:
    """Structured review の痕跡があるか判定する"""
    markers = [
        r'severity\s*[:：]\s*(critical|warning|info)',
        r'auto_fixable\s*[:：]\s*(true|false)',
        r'問題\s*[:：]',
        r'判断ポイント\s*[:：]',
        r'何が起きるか\s*[:：]',
        r'なぜ起きるか\s*[:：]',
        r'変更内容\s*[:：]',
        r'\[(critical|warning|info)\]',
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in markers)


def is_explicit_clean_output(text: str) -> bool:
    """0件を明示している正常なクリーン出力か判定する"""
    block = extract_markdown_review_block(text).strip()
    clean_patterns = [
        r'^##\s*レビュー結果[（(]ループ\s*\d+/5[）)]\s*'
        r'###\s*自動修正可[（(]0件[）)]\s*(?:-\s*問題なし\s*)?'
        r'###\s*要確認[（(]0件[）)]\s*(?:-\s*なし\s*)?$',
        r'^(?:意図:.*\n+)?##\s*自動修正可\s*[（(]0件[）)]\s*(?:-\s*問題なし\s*)?'
        r'##\s*要確認\s*[（(]0件[）)]\s*(?:>\s.*\n|\s)*(?:-\s*なし\s*)?$',
    ]
    return any(re.search(pattern, block, re.MULTILINE | re.DOTALL) for pattern in clean_patterns)


def parse_findings_from_markdown(markdown: str, model: str) -> list[dict]:
    """Step 4 フォーマットの Markdown から findings を抽出"""
    findings = []
    # まずレビューブロックを抽出（前置き除去）
    markdown = extract_markdown_review_block(markdown)

    # セクションで分割: ## 自動修正可, ## 要確認, ## 良い点, ## 対象外
    sections = re.split(r'^## ', markdown, flags=re.MULTILINE)

    for section in sections:
        if not section.strip():
            continue

        section_header = section.split('\n', 1)[0].strip()
        is_auto_fixable = '自動修正可' in section_header
        is_confirmation = '要確認' in section_header
        is_excluded = '対象外' in section_header or '良い点' in section_header

        if is_excluded:
            continue

        if is_auto_fixable:
            # ### ヘッダーで個別 finding を分割
            items = re.split(r'^### ', section, flags=re.MULTILINE)
            for item in items[1:]:  # 最初の要素はセクションヘッダー
                finding = _parse_auto_fixable_item(item, model)
                if finding:
                    findings.append(finding)
                elif has_structured_review_markers(item):
                    findings.extend(_fallback_parse(item, model, auto_fixable_hint=True))

        elif is_confirmation:
            section = section.split('\n', 1)[1] if '\n' in section else ""
            # 要確認セクション先頭の blockquote（運用ルール説明）を除去してから解析
            # ifr.md Step 4 の「堅牢方向の自動選択ルール」等の説明ブロックを finding として誤解析しないため
            section_lines = section.split('\n')
            filtered_lines = []
            in_leading_blockquote = True
            for line in section_lines:
                if in_leading_blockquote:
                    if line.strip().startswith('>') or line.strip() == '':
                        continue  # 先頭の blockquote と空行をスキップ
                    else:
                        in_leading_blockquote = False
                filtered_lines.append(line)
            section = '\n'.join(filtered_lines)

            # 要確認カードの分割（区切り線またはヘッダーで分割）
            cards = re.split(r'─{5,}|^### ', section, flags=re.MULTILINE)
            if len(cards) <= 1:
                # 区切り線なし: セクション全体を1つのカードとして処理
                finding = _parse_confirmation_item(section, model)
                if finding:
                    findings.append(finding)
            else:
                # 全カードを処理（先頭カードも含む）
                for card in cards:
                    finding = _parse_confirmation_item(card, model)
                    if finding:
                        findings.append(finding)
                    elif has_structured_review_markers(card):
                        findings.extend(_fallback_parse(card, model, auto_fixable_hint=False))

        else:
            # レビュー結果（ループ N/5）形式の場合
            if 'レビュー結果' in section_header:
                items = re.split(r'^### ', section, flags=re.MULTILINE)
                for item in items[1:]:
                    item_header = item.split('\n', 1)[0].strip()
                    if '自動修正可' in item_header:
                        # リスト形式の finding を処理
                        sub_findings = _parse_list_findings(item, model, auto_fixable=True)
                        findings.extend(sub_findings)
                    elif '要確認' in item_header:
                        sub_findings = _parse_list_findings(item, model, auto_fixable=False)
                        findings.extend(sub_findings)

    # findings が抽出できなかった場合、行単位でフォールバック解析
    if not findings:
        findings = _fallback_parse(markdown, model)

    return findings


def _parse_auto_fixable_item(text: str, model: str) -> dict | None:
    """自動修正可セクションの個別アイテムを解析"""
    lines = text.strip().split('\n')
    if not lines:
        return None

    title = lines[0].strip()
    full_text = '\n'.join(lines)
    file_path, line_num = parse_file_path_line(full_text)
    severity = parse_severity(full_text)

    # auto_fixable セクション内で severity が明示されていない場合のみ warning をデフォルトとする
    # "severity:" フィールドが存在するなら明示的な info を尊重する
    has_explicit_severity = re.search(r'severity\s*[:：]\s*(critical|warning|info)', full_text, re.IGNORECASE)
    if severity == "info" and not has_explicit_severity:
        severity = "warning"

    return {
        "title": title,
        "file_path": file_path,
        "line": line_num,
        "severity": severity,
        "auto_fixable": True,
        "detected_by": model,
        "raw_text": full_text[:500],  # デバッグ用に先頭500文字保持
    }


def _parse_confirmation_item(text: str, model: str) -> dict | None:
    """要確認セクションの個別アイテムを解析"""
    text = text.strip()
    if not text:
        return None

    # 箇条書きプレフィックスを正規化してからプレースホルダ判定
    normalized = re.sub(r'^(?:[-+*]|\d+\.)\s*', '', text).strip()
    if normalized in ("問題なし", "なし", "None", "N/A", ""):
        return None
    if not has_structured_review_markers(text):
        return None

    # タイトル抽出: "問題:" フィールドまたは最初の行
    title_match = re.search(r'問題\s*[:：]\s*(.+)', text)
    title = title_match.group(1).strip() if title_match else text.split('\n', 1)[0].strip()

    # 判断ポイント抽出
    judgment_match = re.search(r'判断ポイント\s*[:：]\s*(.+?)(?:\n|$)', text)
    judgment = judgment_match.group(1).strip() if judgment_match else ""

    file_path, line_num = parse_file_path_line(text)
    severity = parse_severity(text)

    return {
        "title": title,
        "file_path": file_path,
        "line": line_num,
        "severity": severity,
        "auto_fixable": False,
        "judgment": judgment,
        "detected_by": model,
        "raw_text": text[:500],
    }


def _parse_list_findings(text: str, model: str, auto_fixable: bool) -> list[dict]:
    """リスト形式（- [severity] ...）の findings を項目単位で解析
    番号行の直後に続く `→ 方針:` 行を judgment として抽出する"""
    findings = []
    lines = text.strip().split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith('-') and not line.startswith('+') and not re.match(r'^\d+\.', line):
            i += 1
            continue

        # [severity] パターンを検出（多桁番号リスト 10. 11. ... にも対応）
        sev_match = re.match(r'^(?:[-+]|\d+\.)\s*\[?(critical|warning|info)\]?\s*(.+)', line, re.IGNORECASE)
        if sev_match:
            severity = sev_match.group(1).lower()
            rest = sev_match.group(2).strip()
        else:
            severity = "warning" if auto_fixable else "info"
            rest = re.sub(r'^(?:[-+]|\d+\.)\s*', '', line).strip()

        if not rest:
            i += 1
            continue

        # プレースホルダテキストをスキップ（0件セクションの「問題なし」「なし」等）
        if rest in ("問題なし", "なし", "None", "N/A"):
            i += 1
            continue

        file_path, line_num = parse_file_path_line(rest)
        # @ 記号以前をタイトルとして抽出
        title = re.split(r'\s*@\s*', rest, maxsplit=1)[0].strip()

        # 継続行から judgment（→ 方針: ...）を抽出
        judgment = ""
        j = i + 1
        while j < len(lines):
            next_line = lines[j].strip()
            # 次の項目行（リストマーカーや番号）に達したら終了
            if next_line.startswith('-') or next_line.startswith('+') or re.match(r'^\d+\.', next_line):
                break
            # 「→ 方針:」パターンを検出
            jm = re.match(r'^→\s*方針[:：]\s*(.+)', next_line)
            if jm:
                judgment = jm.group(1).strip()
            j += 1

        findings.append({
            "title": title,
            "file_path": file_path,
            "line": line_num,
            "severity": severity,
            "auto_fixable": auto_fixable,
            "judgment": judgment,
            "detected_by": model,
            "raw_text": line[:500],
        })
        i = j if j > i + 1 else i + 1
    return findings


def _fallback_parse(markdown: str, model: str, auto_fixable_hint: bool = False) -> list[dict]:
    """構造化解析に失敗した場合のフォールバック: 行ベースで finding を推定"""
    findings = []
    blocks = [block.strip() for block in re.split(r'\n\s*\n|─{5,}', markdown) if block.strip()]
    for block in blocks:
        if not has_structured_review_markers(block):
            continue

        severity = parse_severity(block)
        file_path, line_num = parse_file_path_line(block)
        title = ""
        problem_match = re.search(r'問題\s*[:：]\s*(.+)', block)
        if problem_match:
            title = problem_match.group(1).strip()
        else:
            first_labeled = re.search(r'^\s*(?:[-+*]|\d+\.)?\s*\[(critical|warning|info)\]\s*(.+)$', block, re.IGNORECASE | re.MULTILINE)
            if first_labeled:
                title = re.split(r'\s*@\s*', first_labeled.group(2).strip(), maxsplit=1)[0].strip()
            else:
                for line in block.split('\n'):
                    normalized = line.strip()
                    if not normalized or normalized.startswith('>'):
                        continue
                    if re.match(r'^(severity|auto_fixable|詳細|判断ポイント|何が起きるか|なぜ起きるか|変更内容)\s*[:：]', normalized, re.IGNORECASE):
                        continue
                    title = re.sub(r'^(?:[-+*]|\d+\.)\s*', '', normalized).strip()
                    break

        judgment_match = re.search(r'判断ポイント\s*[:：]\s*(.+?)(?:\n|$)', block)
        judgment = judgment_match.group(1).strip() if judgment_match else ""

        if title:
            findings.append({
                "title": title,
                "file_path": file_path,
                "line": line_num,
                "severity": severity,
                "auto_fixable": auto_fixable_hint,
                "judgment": judgment,
                "detected_by": model,
                "raw_text": block[:500],
            })
    return findings


def is_similar_title(title1: str, title2: str, threshold: float = 0.6) -> bool:
    """タイトルの類似度判定"""
    return SequenceMatcher(None, title1.lower(), title2.lower()).ratio() >= threshold


def is_duplicate(f1: dict, f2: dict) -> bool:
    """2つの finding が重複かどうか判定
    条件: 同一ファイル + 同一行(±3) + タイトル類似
    """
    if not f1["file_path"] or not f2["file_path"]:
        return False

    # ファイルパスの正規化（バックスラッシュ→スラッシュ、末尾の空白除去）
    path1 = f1["file_path"].replace("\\", "/").strip()
    path2 = f2["file_path"].replace("\\", "/").strip()
    if path1 != path2:
        return False

    # 行番号の近接判定（±3行）
    if f1["line"] is None or f2["line"] is None:
        return False
    if abs(f1["line"] - f2["line"]) > 3:
        return False

    return is_similar_title(f1["title"], f2["title"])


def merge_findings(all_findings: list[dict], total_model_count: int = 3) -> list[dict]:
    """全 findings を重複排除・マージ"""
    merged = []

    for finding in all_findings:
        matched = False
        for existing in merged:
            if is_duplicate(finding, existing):
                # detected_by をマージ（新しいモデルが追加された場合のみカウント増加）
                if existing["detected_by"] != "all":
                    models = set(existing["detected_by"].split("+"))
                    new_model = finding["detected_by"]
                    if new_model not in models:
                        models.add(new_model)
                        existing["detection_count"] = len(models)
                        sorted_models = sorted(models)
                        if len(sorted_models) == total_model_count:
                            existing["detected_by"] = "all"
                        else:
                            existing["detected_by"] = "+".join(sorted_models)

                # severity: 高い方を採用
                if SEVERITY_ORDER.get(finding["severity"], 0) > SEVERITY_ORDER.get(existing["severity"], 0):
                    existing["severity"] = finding["severity"]

                # auto_fixable: 食い違い時は false を採用（慎重側）
                if not finding["auto_fixable"]:
                    existing["auto_fixable"] = False

                # 行番号: より具体的な方を採用
                if existing["line"] is None and finding["line"] is not None:
                    existing["line"] = finding["line"]

                # ファイルパス: より具体的な方を採用
                if not existing["file_path"] and finding["file_path"]:
                    existing["file_path"] = finding["file_path"]

                # judgment: 存在する方を採用（両方ある場合は長い方を優先）
                existing_j = existing.get("judgment", "")
                finding_j = finding.get("judgment", "")
                if not existing_j and finding_j:
                    existing["judgment"] = finding_j
                elif existing_j and finding_j and len(finding_j) > len(existing_j):
                    existing["judgment"] = finding_j

                matched = True
                break

        if not matched:
            finding["detection_count"] = 1
            merged.append(finding)

    return merged


def format_output(merged: list[dict], output_format: str = "json") -> str:
    """マージ結果をフォーマット"""
    if output_format == "json":
        # raw_text はデバッグ用なので出力から除外
        clean = []
        for f in merged:
            item = {k: v for k, v in f.items() if k != "raw_text"}
            clean.append(item)
        return json.dumps(clean, ensure_ascii=False, indent=2)

    elif output_format == "markdown":
        # rfl Step 2 の提示フォーマットに変換
        # info以下は自動修正・要確認を問わず「対象外」（rfl Step 2仕様準拠）
        excluded = [f for f in merged if f["severity"] == "info"]
        auto_fix = [f for f in merged if f["auto_fixable"] and f["severity"] != "info"]
        confirm = [f for f in merged if not f["auto_fixable"] and f["severity"] != "info"]

        lines = []
        # 0件でも明示的にセクションを出力（空文字を返さない）
        # NOTE: Markdown出力には auto_fixable フィールドを明示的に含めない。
        # rfl Step 2 の分類ルールにより、「## 自動修正可」セクション = auto_fixable: true、
        # 「## 要確認」セクション = auto_fixable: false とセクション所属から暗黙的に判定する設計。
        # JSON出力には auto_fixable を明示的に含む（非対称だが意図的）。
        lines.append(f"## 自動修正可（{len(auto_fix)}件）")
        if auto_fix:
            for f in auto_fix:
                loc = f"{f['file_path']}:{f['line']}" if f['file_path'] and f['line'] else "不明"
                trust = " [高信頼]" if f.get("detection_count", 1) >= 2 else ""
                lines.append(f"- [{f['severity']}] {f['title']} @ {loc} (detected_by: {f['detected_by']}){trust}")
        else:
            lines.append("- 問題なし")
        lines.append("")

        lines.append(f"## 要確認（{len(confirm)}件）")
        if confirm:
            for i, f in enumerate(confirm, 1):
                loc = f"{f['file_path']}:{f['line']}" if f['file_path'] and f['line'] else "不明"
                judgment = f.get("judgment", "")
                trust = " [高信頼]" if f.get("detection_count", 1) >= 2 else ""
                lines.append(f"{i}. [{f['severity']}] {f['title']} @ {loc} (detected_by: {f['detected_by']}){trust}")
                if judgment:
                    lines.append(f"   → 方針: {judgment}")
        else:
            lines.append("- なし")
        lines.append("")

        if excluded:
            lines.append(f"## 対象外（Info以下）（{len(excluded)}件）")
            for f in excluded:
                lines.append(f"- {f['title']} (detected_by: {f['detected_by']})")
            lines.append("")

        return '\n'.join(lines)

    return ""


def main():
    parser = argparse.ArgumentParser(description="並列レビュー結果のマージ（2〜Nモデル対応）")
    parser.add_argument("--opus", type=str, help="Opus レビュー出力ファイル")
    parser.add_argument("--glm", type=str, help="GLM レビュー出力ファイル")
    parser.add_argument("--codex", type=str, help="Codex レビュー出力ファイル")
    parser.add_argument("--input", type=str, action="append", dest="inputs", metavar="NAME:FILE",
                        help="任意のモデル入力（複数回指定可能。例: --input glm-a:/tmp/glm-a.md）")
    parser.add_argument("-o", "--output", type=str, help="出力ファイル（省略時は stdout）")
    parser.add_argument("--format", choices=["json", "markdown"], default="json",
                        help="出力フォーマット（default: json）")
    parser.add_argument("--stats", action="store_true", help="マージ統計を stderr に出力")
    args = parser.parse_args()

    # 少なくとも1つのモデル出力が必要
    model_files = {}
    if args.opus:
        model_files["opus"] = args.opus
    if args.glm:
        model_files["glm"] = args.glm
    if args.codex:
        model_files["codex"] = args.codex
    # --input NAME:FILE 形式の可変入力を追加
    if args.inputs:
        for entry in args.inputs:
            if ":" not in entry:
                print(f"WARNING: --input の形式が不正です（NAME:FILE が必要）: {entry}", file=sys.stderr)
                continue
            name, filepath = entry.split(":", 1)
            if name in model_files:
                print(f"WARNING: --input の名前 '{name}' が既存キーと衝突しています。"
                      f"既存: {model_files[name]} → 上書き: {filepath}", file=sys.stderr)
            model_files[name] = filepath

    if not model_files:
        print("ERROR: 少なくとも1つのモデル出力ファイルを指定してください", file=sys.stderr)
        sys.exit(1)

    # 全モデルの findings を収集
    all_findings = []
    model_counts = {}
    explicit_clean_models = set()
    for model, filepath in model_files.items():
        path = Path(filepath)
        if not path.exists():
            print(f"WARNING: {model} のファイルが見つかりません: {filepath}", file=sys.stderr)
            continue

        content = path.read_text(encoding="utf-8")

        # 空出力ガード: 空文字列・空白のみの出力は「成功0件」ではなく「失敗」として扱う
        if not content.strip():
            print(f"WARNING: {model} の出力が空です。失敗としてスキップします", file=sys.stderr)
            continue

        # エラーマーカー検出: 失敗モデルを参加モデル数から除外
        # （成功した全モデルが同一指摘を出した場合に正しく detected_by: all にするため）
        if content.strip().startswith("## エラー") or content.strip().startswith("## error"):
            print(f"INFO: {model} はエラー出力のためスキップします", file=sys.stderr)
            continue

        findings = parse_findings_from_markdown(content, model)
        explicit_clean = is_explicit_clean_output(content)
        if not findings and not explicit_clean:
            print(f"WARNING: {model} の出力を structured finding に変換できませんでした。非準拠出力としてスキップします", file=sys.stderr)
            continue
        if explicit_clean:
            explicit_clean_models.add(model)
        model_counts[model] = len(findings)
        all_findings.extend(findings)

    # 全モデル失敗ガード: 成功モデルが0件なら「クリーン」ではなく「エラー」
    if len(model_counts) == 0:
        print("ERROR: 成功したモデルが0件です。全モデルがエラーまたはファイル未発見です", file=sys.stderr)
        print("## エラー\n\n全モデルのレビューが失敗しました。個別モデルの出力を確認してください。", file=sys.stdout)
        sys.exit(1)

    merged = []
    if not all_findings:
        if not explicit_clean_models:
            print("WARNING: 全モデルから findings を抽出できませんでした", file=sys.stderr)
        result = format_output([], args.format)
    else:
        # 実際に結果を返したモデル数を使用（部分失敗時に正しく "all" 判定するため）
        merged = merge_findings(all_findings, total_model_count=len(model_counts))
        result = format_output(merged, args.format)

    # 統計出力
    if args.stats:
        total_before = len(all_findings)
        total_after = len(merged) if all_findings else 0
        dedup_count = total_before - total_after
        multi_detect = sum(1 for f in (merged if all_findings else []) if f.get("detection_count", 1) >= 2)
        print(f"--- マージ統計 ---", file=sys.stderr)
        for model, count in model_counts.items():
            print(f"  {model}: {count}件", file=sys.stderr)
        print(f"  合計(マージ前): {total_before}件", file=sys.stderr)
        print(f"  合計(マージ後): {total_after}件", file=sys.stderr)
        print(f"  重複排除: {dedup_count}件", file=sys.stderr)
        print(f"  2モデル以上検出: {multi_detect}件", file=sys.stderr)

    # 出力
    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
        print(f"OK: {args.output} に出力しました", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()
