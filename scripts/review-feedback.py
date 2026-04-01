#!/usr/bin/env python3
"""Review Feedback Learning System - SQLiteベースのレビュー指摘管理CLI。

各レビューコマンド(brutal-review, qc, pr-review等)の指摘結果を記録し、
誤検知パターンを学習してレビュー品質を向上させる。

Usage:
    python review-feedback.py record --reviewer "brutal-review" --findings '[...]'
    python review-feedback.py resolve --ids "1,2" --resolution "accepted"
    python review-feedback.py query --reviewer "brutal-review" --resolution "rejected_wrong"
    python review-feedback.py analyze --reviewer "brutal-review"
    python review-feedback.py summary
    python review-feedback.py inject --reviewer "brutal-review"
    python review-feedback.py check-open-sessions
    python review-feedback.py close-session --reviewer "brutal-review" --reason "no-findings"
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Windows 環境で cp932 stdout に日本語を出力するための UTF-8 強制
# reconfigure: TextIOWrapper と異なり既存バッファを置換しないためより安全
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = Path.home() / ".claude" / "review-feedback.db"
SESSION_ID_PATH = Path.home() / ".session-id"

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    repo_root TEXT,
    reviewer TEXT NOT NULL,
    finding_summary TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('critical','high','warning','info','nitpick')),
    category TEXT,
    resolution TEXT NOT NULL DEFAULT 'pending'
        CHECK (resolution IN ('pending','accepted','rejected_intentional','rejected_wrong','fixed','stale')),
    abstracted_pattern TEXT,
    project TEXT,
    file_path TEXT,
    score INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    resolved_at TEXT,
    dismissed INTEGER NOT NULL DEFAULT 0,
    dismissed_at TEXT,
    dismissed_by TEXT,
    fp_reason TEXT,
    injected_count INTEGER NOT NULL DEFAULT 0,
    last_injected TEXT
);
"""

# inject時にopenセッションを作り、record/close-sessionで閉じるテーブル
SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    reviewer TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    closed_at TEXT,
    findings_count INTEGER DEFAULT 0,
    close_reason TEXT
);
"""

# インデックス定義をテーブル DDL から分離している理由: 可読性のため。
# テーブル DDL と合わせて毎回 execute() で冪等適用する（既存 DB にも新インデックスが自動反映される）。
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_findings_reviewer ON findings(reviewer);
CREATE INDEX IF NOT EXISTS idx_findings_resolution ON findings(resolution);
CREATE INDEX IF NOT EXISTS idx_findings_reviewer_resolution ON findings(reviewer, resolution);
CREATE INDEX IF NOT EXISTS idx_findings_file_path ON findings(file_path);
CREATE INDEX IF NOT EXISTS idx_findings_pending ON findings(resolution, severity, created_at);
CREATE INDEX IF NOT EXISTS idx_repo_file ON findings(repo_root, file_path);
CREATE INDEX IF NOT EXISTS idx_review_sessions_status ON review_sessions(status);
"""


def get_connection() -> sqlite3.Connection:
    """DB接続を取得。テーブル未作成なら自動作成。WALモード有効。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    # CREATE TABLE/INDEX は IF NOT EXISTS で冪等 → 毎回実行して既存 DB にも変更を反映
    # executescript() は暗黙 COMMIT を発行するため、個別 execute() で代替してトランザクション汚染を防ぐ
    for ddl in (SCHEMA + SESSIONS_SCHEMA + INDEXES).split(";"):
        stmt = ddl.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()
    return conn


def get_session_id() -> Optional[str]:
    """~/.session-idから現在のセッションIDを読み取る。"""
    try:
        return SESSION_ID_PATH.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, IOError):
        return None


def get_project_name() -> Optional[str]:
    """プロジェクト名を推定。git リポジトリルートのディレクトリ名を優先し、なければ cwd 名を返す。"""
    root = get_repo_root()
    if root:
        return Path(root).name
    try:
        return Path.cwd().name
    except Exception:
        return None


def get_repo_root() -> Optional[str]:
    """git rev-parse --show-toplevel でリポジトリルートを取得。パス区切りは / に正規化。"""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return result.stdout.strip().replace("\\", "/")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


# --- record ---
def cmd_record(args):
    """指摘事項をDBに記録 + openセッションをclosedに更新する。"""
    try:
        findings = json.loads(args.findings)
    except json.JSONDecodeError as e:
        print(f"Error: --findings のJSONが不正: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(findings, list):
        print("Error: --findings はJSON配列で指定してください", file=sys.stderr)
        sys.exit(1)

    if not findings:
        print(f"Warning: findingsが空です（reviewer={args.reviewer}）。記録するfindingがありません", file=sys.stderr)

    session_id = args.session_id or get_session_id()
    project = args.project or get_project_name()
    repo_root = args.repo_root if hasattr(args, 'repo_root') and args.repo_root else get_repo_root()

    conn = get_connection()
    inserted_ids = []
    try:
        for f in findings:
            summary = f.get("summary", "")
            if not summary:
                print("Warning: summaryが空のfindingをスキップ", file=sys.stderr)
                continue

            severity = f.get("severity", "info")
            if severity not in ("critical", "high", "warning", "info", "nitpick"):
                severity = "info"

            cursor = conn.execute(
                """INSERT INTO findings (session_id, repo_root, reviewer, finding_summary, severity, category, project, file_path, score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    repo_root,
                    args.reviewer,
                    summary,
                    severity,
                    f.get("category"),
                    project,
                    f.get("file_path"),
                    f.get("score"),
                ),
            )
            inserted_ids.append(cursor.lastrowid)

        # openセッションをclosedに更新
        # session_id がある場合は同一セッションのみを対象にする（並行実行・複数リポジトリでの誤閉じを防止）
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        session_clause = "AND session_id=?" if session_id else ""
        session_param = [session_id] if session_id else []
        updated = conn.execute(
            f"""UPDATE review_sessions
               SET status='closed', closed_at=?, findings_count=?, close_reason='recorded'
               WHERE id = (
                   SELECT id FROM review_sessions
                   WHERE reviewer=? AND status='open' {session_clause}
                   ORDER BY started_at DESC LIMIT 1
               )""",
            [now, len(inserted_ids), args.reviewer] + session_param,
        ).rowcount
        if updated == 0:
            print("Warning: 対応するopenセッションが見つかりません（後方互換で記録は継続）", file=sys.stderr)

        conn.commit()
    finally:
        conn.close()

    print(json.dumps({"inserted_ids": inserted_ids}))


# --- resolve ---
def cmd_resolve(args):
    """findingのresolutionを更新する。"""
    try:
        ids = [int(x.strip()) for x in args.ids.split(",")]
    except ValueError:
        print("Error: --ids はカンマ区切りの整数で指定してください", file=sys.stderr)
        sys.exit(1)

    if args.resolution not in ("accepted", "rejected_intentional", "rejected_wrong", "fixed", "stale"):
        print("Error: --resolution は accepted/rejected_intentional/rejected_wrong/fixed/stale のいずれか", file=sys.stderr)
        sys.exit(1)

    conn = get_connection()
    try:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        placeholders = ",".join("?" * len(ids))

        if args.pattern and args.resolution == "rejected_wrong":
            cursor = conn.execute(
                f"UPDATE findings SET resolution=?, resolved_at=?, abstracted_pattern=? WHERE id IN ({placeholders})",
                [args.resolution, now, args.pattern] + ids,
            )
        else:
            cursor = conn.execute(
                f"UPDATE findings SET resolution=?, resolved_at=? WHERE id IN ({placeholders})",
                [args.resolution, now] + ids,
            )
        updated = cursor.rowcount
        conn.commit()
    finally:
        conn.close()

    print(json.dumps({"updated": updated}))


# --- query ---
def cmd_query(args):
    """findingsを条件付きで検索する。"""
    conditions = []
    params = []

    if args.reviewer:
        conditions.append("reviewer = ?")
        params.append(args.reviewer)
    if args.resolution:
        conditions.append("resolution = ?")
        params.append(args.resolution)
    if args.severity:
        conditions.append("severity = ?")
        params.append(args.severity)
    if args.project:
        conditions.append("project = ?")
        params.append(args.project)
    if args.since:
        conditions.append("created_at >= ?")
        params.append(args.since)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit = args.limit or 50

    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM findings {where} ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    finally:
        conn.close()

    results = [dict(row) for row in rows]
    print(json.dumps(results, ensure_ascii=False, indent=2))


# --- analyze ---
def cmd_analyze(args):
    """rejected_wrongのパターンを集計する。"""
    conditions = ["resolution = 'rejected_wrong'"]
    params = []

    if args.reviewer:
        conditions.append("reviewer = ?")
        params.append(args.reviewer)

    where = f"WHERE {' AND '.join(conditions)}"
    min_count = args.min_count or 1

    conn = get_connection()
    try:
        # パターンが記録されているもの: パターンでグループ化
        rows_with_pattern = conn.execute(
            f"""SELECT abstracted_pattern, reviewer, COUNT(*) as count,
                       GROUP_CONCAT(finding_summary, CHAR(0)) as examples
                FROM findings
                {where} AND abstracted_pattern IS NOT NULL AND abstracted_pattern != ''
                GROUP BY abstracted_pattern, reviewer
                HAVING COUNT(*) >= ?
                ORDER BY count DESC""",
            params + [min_count],
        ).fetchall()

        # パターン未記録のもの: summaryでグループ化（類似判定の簡易版）
        rows_without_pattern = conn.execute(
            f"""SELECT finding_summary, reviewer, COUNT(*) as count
                FROM findings
                {where} AND (abstracted_pattern IS NULL OR abstracted_pattern = '')
                GROUP BY finding_summary, reviewer
                HAVING COUNT(*) >= ?
                ORDER BY count DESC""",
            params + [min_count],
        ).fetchall()
    finally:
        conn.close()

    patterns = []
    for row in rows_with_pattern:
        patterns.append({
            "pattern": row["abstracted_pattern"],
            "reviewer": row["reviewer"],
            "count": row["count"],
            "examples": row["examples"].split("\x00")[:3],
        })
    for row in rows_without_pattern:
        patterns.append({
            "pattern": f"(未抽象化) {row['finding_summary']}",
            "reviewer": row["reviewer"],
            "count": row["count"],
            "examples": [row["finding_summary"]],
        })

    print(json.dumps(patterns, ensure_ascii=False, indent=2))


# --- summary ---
def cmd_summary(args):
    """全体統計を表示する。"""
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]

        # レビュアー別
        by_reviewer = conn.execute(
            "SELECT reviewer, COUNT(*) as cnt FROM findings GROUP BY reviewer ORDER BY cnt DESC"
        ).fetchall()

        # resolution別
        by_resolution = conn.execute(
            "SELECT resolution, COUNT(*) as cnt FROM findings GROUP BY resolution ORDER BY cnt DESC"
        ).fetchall()

        # レビュアー別の偽陽性率
        fp_rates = conn.execute(
            """SELECT reviewer,
                      COUNT(*) as total,
                      SUM(CASE WHEN resolution='rejected_wrong' THEN 1 ELSE 0 END) as false_positives
               FROM findings
               WHERE resolution != 'pending'
               GROUP BY reviewer"""
        ).fetchall()
    finally:
        conn.close()

    lines = [
        "Review Feedback Summary",
        "=" * 40,
        f"Total findings: {total}",
        "",
        "By reviewer:",
    ]
    for row in by_reviewer:
        lines.append(f"  {row['reviewer']}: {row['cnt']}")

    lines.append("")
    lines.append("By resolution:")
    for row in by_resolution:
        lines.append(f"  {row['resolution']}: {row['cnt']}")

    if fp_rates:
        lines.append("")
        lines.append("False positive rate (resolved findings only):")
        for row in fp_rates:
            total_resolved = row["total"]
            fp = row["false_positives"]
            rate = (fp / total_resolved * 100) if total_resolved > 0 else 0
            lines.append(f"  {row['reviewer']}: {rate:.0f}% ({fp}/{total_resolved})")

    print("\n".join(lines))


# --- inject ---
def cmd_inject(args):
    """過去の誤検知パターン出力 + レビューセッションをopenで作成する。"""
    session_id = args.session_id if args.session_id else get_session_id()

    conn = get_connection()
    try:
        # rejected_wrongのパターンを取得
        rows = conn.execute(
            """SELECT abstracted_pattern, COUNT(*) as count
               FROM findings
               WHERE reviewer = ? AND resolution = 'rejected_wrong'
                     AND abstracted_pattern IS NOT NULL AND abstracted_pattern != ''
               GROUP BY abstracted_pattern
               ORDER BY count DESC
               LIMIT 10""",
            (args.reviewer,),
        ).fetchall()

        # パターン未記録だが rejected_wrong のもの
        rows_no_pattern = conn.execute(
            """SELECT finding_summary, COUNT(*) as count
               FROM findings
               WHERE reviewer = ? AND resolution = 'rejected_wrong'
                     AND (abstracted_pattern IS NULL OR abstracted_pattern = '')
               GROUP BY finding_summary
               ORDER BY count DESC
               LIMIT 5""",
            (args.reviewer,),
        ).fetchall()

        # 同一reviewerの既存openセッションを先にcloseする（重複防止）
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            """UPDATE review_sessions SET status='closed', closed_at=?, close_reason='superseded'
               WHERE reviewer=? AND status='open'""",
            (now, args.reviewer),
        )

        # レビューセッションをopenで作成（構造的強制の起点）
        conn.execute(
            "INSERT INTO review_sessions (session_id, reviewer, status) VALUES (?, ?, 'open')",
            (session_id, args.reviewer),
        )
        conn.commit()
    finally:
        conn.close()

    if not rows and not rows_no_pattern:
        # パターンなし: セッションは作成済みだが出力はなし
        return

    lines = [
        f"## Known False Positive Patterns ({args.reviewer})",
        "以下は過去にユーザーが誤検知として却下したパターン。指摘前に再考すること:",
    ]

    for row in rows:
        lines.append(f"- {row['abstracted_pattern']} ({row['count']}回)")

    if rows_no_pattern:
        lines.append("")
        lines.append("以下は却下されたが未抽象化の指摘:")
        for row in rows_no_pattern:
            lines.append(f"- {row['finding_summary']} ({row['count']}回)")

    print("\n".join(lines))


# --- check-open-sessions ---
def cmd_check_open_sessions(args):
    """未完了のレビューセッション（status='open'）を返す。24時間超のセッションは自動GC。"""
    conn = get_connection()
    try:
        # 24時間超のopenセッションをstaleとして自動close（GC）
        stale_cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            """UPDATE review_sessions SET status='closed', closed_at=?, close_reason='stale'
               WHERE status='open' AND started_at < ?""",
            (now, stale_cutoff),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT id, session_id, reviewer, started_at FROM review_sessions WHERE status='open' ORDER BY started_at DESC"
        ).fetchall()
    finally:
        conn.close()

    results = [dict(row) for row in rows]
    print(json.dumps(results, ensure_ascii=False, indent=2))


# --- close-session ---
def cmd_close_session(args):
    """findings 0件でセッションを閉じる（問題なし / 中断時）。"""
    session_id = getattr(args, "session_id", None) or get_session_id()
    conn = get_connection()
    try:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        reason = args.reason or "manual-close"
        # session_id がある場合は同一セッションのみを対象にする（並行実行・複数リポジトリでの誤閉じを防止）
        session_clause = "AND session_id=?" if session_id else ""
        session_param = [session_id] if session_id else []
        updated = conn.execute(
            f"""UPDATE review_sessions
               SET status='closed', closed_at=?, findings_count=0, close_reason=?
               WHERE id = (
                   SELECT id FROM review_sessions
                   WHERE reviewer=? AND status='open' {session_clause}
                   ORDER BY started_at DESC LIMIT 1
               )""",
            [now, reason, args.reviewer] + session_param,
        ).rowcount
        conn.commit()
    finally:
        conn.close()

    if updated == 0:
        print(json.dumps({"closed": False, "message": "対応するopenセッションが見つかりません"}))
    else:
        print(json.dumps({"closed": True, "reviewer": args.reviewer, "reason": reason}))


# --- dismiss ---
def cmd_dismiss(args):
    """finding を dismissed にする（ユーザー承認フロー）。

    dismissed は人間のみが承認できる。Claude の自己判断での dismissed 処理は禁止。
    このコマンドは dismissed_by='user' のみを設定するため、
    Claude が直接実行しても 'user' にはなれず安全性が保証される。
    """
    ids_str = args.ids
    fp_reason = args.fp_reason
    interactive = args.interactive
    if interactive is None:
        # TTY 判定: パイプ・スクリプト呼び出し時は非対話モードに自動切替
        interactive = sys.stdin.isatty()

    try:
        ids = [int(x.strip()) for x in ids_str.split(",") if x.strip()]
    except ValueError:
        print("Error: --ids はカンマ区切りの整数 ID で指定してください", file=sys.stderr)
        sys.exit(1)

    if not ids:
        print("Error: ID が指定されていません", file=sys.stderr)
        sys.exit(1)

    # 例外時の NameError を防ぐため try ブロック外で初期化
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    updated = 0
    conn = get_connection()
    try:
        # 対象 finding を表示して確認を求める
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, severity, category, finding_summary, file_path FROM findings WHERE id IN ({placeholders})",
            ids,
        ).fetchall()

        if not rows:
            print(f"Error: 指定された ID {ids} に該当する finding が見つかりません", file=sys.stderr)
            sys.exit(1)

        print(f"\n以下の {len(rows)} 件を dismissed にします:")
        for r in rows:
            print(f"  [{r['severity'].upper()}] ID={r['id']} {r['category'] or '?'}: {r['finding_summary']}")
            if r['file_path']:
                print(f"    ファイル: {r['file_path']}")

        # インタラクティブモード: 理由未指定なら入力を促す
        if interactive and not fp_reason:
            try:
                fp_reason = input("\nfalse positive の理由を入力してください（空白でスキップ）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nキャンセルされました", file=sys.stderr)
                sys.exit(1)

        # インタラクティブモード: 最終確認
        if interactive:
            try:
                confirm = input(f"\n{len(rows)} 件を dismissed にしますか？ [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nキャンセルされました", file=sys.stderr)
                sys.exit(1)
            if confirm not in ("y", "yes"):
                print("キャンセルされました")
                sys.exit(0)

        updated = conn.execute(
            f"""UPDATE findings
               SET dismissed = 1,
                   dismissed_by = 'user',
                   dismissed_at = ?,
                   fp_reason = ?
               WHERE id IN ({placeholders})
                 AND dismissed = 0""",
            (now, fp_reason or None, *ids),
        ).rowcount
        conn.commit()
    finally:
        conn.close()

    # updated < len(ids) は既に dismissed 済みの finding が含まれていることを示す
    already_done = len(ids) - updated
    if already_done > 0:
        print(
            f"Warning: {already_done} 件はすでに dismissed 済みのためスキップしました"
            f"（dismissed: {updated} / 指定: {len(ids)}）",
            file=sys.stderr,
        )

    print(json.dumps({
        "dismissed": updated,
        "ids": ids,
        "fp_reason": fp_reason or None,
        "dismissed_by": "user",
        "dismissed_at": now,
    }))


# --- gc-stale ---
def cmd_gc_stale(args):
    """90日以上 pending の findings を stale に自動遷移させる（TTL ベースアーカイブ）。"""
    stale_days = args.days or 90
    cutoff = (datetime.now() - timedelta(days=stale_days)).strftime("%Y-%m-%dT%H:%M:%S")
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    conn = get_connection()
    try:
        updated = conn.execute(
            """UPDATE findings
               SET resolution = 'stale', resolved_at = ?
               WHERE resolution = 'pending'
                 AND created_at < ?""",
            (now, cutoff),
        ).rowcount
        conn.commit()
    finally:
        conn.close()

    print(json.dumps({"stale_count": updated, "cutoff_days": stale_days}))


# --- main ---
def main():
    parser = argparse.ArgumentParser(
        description="Review Feedback Learning System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="利用可能なコマンド")

    # record
    p_record = subparsers.add_parser("record", help="findingを記録")
    p_record.add_argument("--reviewer", required=True, help="レビュアー名")
    p_record.add_argument("--findings", required=True, help="JSON配列: [{summary, severity, category?, file_path?, score?}]")
    p_record.add_argument("--session-id", help="セッションID（省略時は~/.session-idから取得）")
    p_record.add_argument("--project", help="プロジェクト名（省略時はcwd名）")
    p_record.add_argument("--repo-root", help="リポジトリルート（省略時はgit rev-parse --show-toplevel）")
    p_record.set_defaults(func=cmd_record)

    # resolve
    p_resolve = subparsers.add_parser("resolve", help="findingのresolutionを更新")
    p_resolve.add_argument("--ids", required=True, help="カンマ区切りのfinding ID")
    p_resolve.add_argument("--resolution", required=True, choices=["accepted", "rejected_intentional", "rejected_wrong", "fixed", "stale"])
    p_resolve.add_argument("--pattern", help="抽象化パターン（rejected_wrong時に使用）")
    p_resolve.set_defaults(func=cmd_resolve)

    # query
    p_query = subparsers.add_parser("query", help="findingsを検索")
    p_query.add_argument("--reviewer", help="レビュアーで絞り込み")
    p_query.add_argument("--resolution", help="resolutionで絞り込み")
    p_query.add_argument("--severity", help="severityで絞り込み")
    p_query.add_argument("--project", help="プロジェクトで絞り込み")
    p_query.add_argument("--since", help="この日時以降のfindingsのみ（ISO 8601）")
    p_query.add_argument("--limit", type=int, default=50, help="最大件数（デフォルト50）")
    p_query.set_defaults(func=cmd_query)

    # analyze
    p_analyze = subparsers.add_parser("analyze", help="誤検知パターンを分析")
    p_analyze.add_argument("--reviewer", help="レビュアーで絞り込み")
    p_analyze.add_argument("--min-count", type=int, default=1, help="最小出現回数（デフォルト1）")
    p_analyze.set_defaults(func=cmd_analyze)

    # summary
    p_summary = subparsers.add_parser("summary", help="全体統計を表示")
    p_summary.set_defaults(func=cmd_summary)

    # inject
    p_inject = subparsers.add_parser("inject", help="レビュアー向け誤検知パターンを出力 + セッションopen")
    p_inject.add_argument("--reviewer", required=True, help="レビュアー名")
    p_inject.add_argument("--session-id", help="セッションID（省略時は~/.session-idから取得）")
    p_inject.set_defaults(func=cmd_inject)

    # check-open-sessions
    p_check = subparsers.add_parser("check-open-sessions", help="未完了のレビューセッションを表示")
    p_check.set_defaults(func=cmd_check_open_sessions)

    # close-session
    p_close = subparsers.add_parser("close-session", help="findings 0件でセッションを閉じる")
    p_close.add_argument("--reviewer", required=True, help="レビュアー名")
    p_close.add_argument("--reason", help="クローズ理由（例: no-findings, cancelled）")
    p_close.add_argument("--session-id", help="セッションID（省略時は~/.session-idから取得）")
    p_close.set_defaults(func=cmd_close_session)

    # dismiss（ユーザー承認フロー）
    p_dismiss = subparsers.add_parser(
        "dismiss",
        help="finding を dismissed にする（ユーザー承認フロー・dismissed_by=user のみ）",
    )
    p_dismiss.add_argument("--ids", required=True, help="カンマ区切りの finding ID")
    p_dismiss.add_argument("--fp-reason", help="false positive の理由（省略可）")
    p_dismiss.add_argument(
        "--interactive",
        action="store_const",
        const=True,
        default=None,
        help="インタラクティブモード: 理由入力と最終確認を促す（デフォルト: TTY判定で自動切替）",
    )
    p_dismiss.add_argument(
        "--no-interactive",
        dest="interactive",
        action="store_false",
        help="インタラクティブモードを無効化（スクリプト呼び出し用）",
    )
    p_dismiss.set_defaults(func=cmd_dismiss)

    # gc-stale（TTL ベースアーカイブ: 90日以上 pending → stale）
    p_gc = subparsers.add_parser("gc-stale", help="90日以上 pending の findings を stale に遷移")
    p_gc.add_argument("--days", type=int, default=90, help="stale 判定の日数（デフォルト: 90）")
    p_gc.set_defaults(func=cmd_gc_stale)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
