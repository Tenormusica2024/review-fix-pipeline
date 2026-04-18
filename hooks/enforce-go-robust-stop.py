"""
enforce-go-robust-stop.py

Stop hook: /ifr, /rfl が出した「要確認」を
/go-robust が処理する前にユーザーに返すのを防ぐ。

検知ロジック（structured state 主導・marker scoring は補助）:
1. UserPromptSubmit hook (enforce-go-robust-submit.py) が記録した
   session state を読む
2. last_review_command 以降に last_go_robust が走っていなければ未処理
3. かつ last_assistant_message に要確認マーカー（## 要確認 + severity + auto_fixable: false など）
   が含まれていれば、pending とみなす
4. pending なら decision:"block" で /go-robust 実行を強制

ループ防止:
- stop_hook_active (built-in) → pass（無限継続防止）
- bypass_once (ユーザーが /skip-go-robust-once または --no-go-robust) → 1回だけ pass
- enforced_count >= MAX_ENFORCE → block を継続し、明示 bypass（/skip-go-robust-once
  or --no-go-robust）または /go-robust 実行を要求する（silent pass はしない）
- /go-robust 自身の出力は検知対象外（last_review_command が /go-robust ではないため自動的に除外）
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# pythonw guard
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


STATE_DIR = Path.home() / ".claude" / "state" / "go-robust-enforce"

# session_id に使える文字（ファイル名として安全な集合のみ許可）。
# submit hook (_state_path) と同一ルールに揃えることで、Stop hook 側だけ
# 緩い文字集合を許すことによるパストラバーサルを防ぐ。
SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

# 同一レビューサイクル内での強制回数上限（安全弁）
MAX_ENFORCE = 2

# 要確認セクションの検出マーカー（IFR Step 4 フォーマット準拠）
# 公開 README に英語例 (## Requires confirmation) を載せている以上、日本語と英語の両方を受理する。
# どちらか片方だけだと README 例に従ったレビュー出力を Stop hook が検知できず /go-robust が発火しない。
# また rfl は `## 要確認（ループ中に蓄積された項目）` や `### 要確認（Y件）` のような件数付き・
# 補足付きヘッダーも使う。そのため以下を全て受理する:
#   - `## 要確認` / `## Requires confirmation`（ベース）
#   - `### 要確認` / `### Requires confirmation`（rfl 集約出力）
#   - `## 要確認（N件）` `## Requires confirmation (details)` 等の括弧補足付き
MARKER_HEADER = re.compile(
    r"^\s*#{2,4}\s*(?:要確認|Requires\s+confirmation)(?:\s*[（(][^)）\n]*[)）])?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
MARKER_SEVERITY = re.compile(
    r"^\s*severity\s*:\s*(critical|warning|info)",
    re.MULTILINE | re.IGNORECASE,
)
MARKER_AUTOFIX_FALSE = re.compile(r"^\s*auto_fixable\s*:\s*false", re.MULTILINE | re.IGNORECASE)
# U+2500 BOX DRAWINGS LIGHT HORIZONTAL の連続（5文字以上）
MARKER_DIVIDER = re.compile(r"─{5,}")
# rfl 集約出力・merge_parallel_reviews.py の Markdown 出力が使う番号 / 箇条書きリスト形式:
#   例: `1. [warning] ...` / `- [critical] ...`
# このフォーマットには `auto_fixable: false` も罫線も含まれないため、
# 従来のマーカーだけでは Stop hook を素通りしていた。ヘッダー `## 要確認` と組み合わせて検知する。
MARKER_LIST_SEVERITY = re.compile(
    r"^\s*(?:[-+*]|\d+\.)\s*\[?(critical|warning|info)\]?\s+",
    re.MULTILINE | re.IGNORECASE,
)


class UnsafeSessionId(Exception):
    """session_id が安全な文字集合に収まらないことを示す。

    fail-closed 設計: None を返して state なし扱いで通すと、不正 session_id で
    enforcement を簡単に無効化できてしまう。検知可能な例外として伝搬させ、
    main() で block に倒す。
    """


def _state_path(session_id: str) -> Path | None:
    """session_id から state ファイルのパスを組み立てる。

    submit hook 側と同じ検証ロジック: 安全な文字集合に収まらない session_id は拒否し、
    resolve() 後に STATE_DIR 配下であることも確認してパストラバーサルを防ぐ。

    Raises:
        UnsafeSessionId: session_id が SAFE_SESSION_ID_PATTERN にマッチしない場合。
    """
    if not SAFE_SESSION_ID_PATTERN.match(session_id):
        sys.stderr.write(f"[enforce-go-robust-stop] unsafe session_id rejected: {session_id!r}\n")
        raise UnsafeSessionId
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = (STATE_DIR / f"{session_id}.json").resolve()
    try:
        path.relative_to(STATE_DIR.resolve())
    except ValueError:
        sys.stderr.write(f"[enforce-go-robust-stop] state path escaped STATE_DIR: {path}\n")
        return None
    return path


class StateCorrupted(Exception):
    """state ファイルは存在するが読めなかった（JSON 破損等）ことを示す。

    fail-closed 設計: submit hook が書いた直後に stop hook が読むレース等で
    壊れた JSON を拾った場合、silently pass すると enforcement guarantee が
    失われる。検知可能な例外として伝搬させ、main() でブロックに倒す。
    """


def load_state(session_id: str) -> dict | None:
    path = _state_path(session_id)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # path / session_id を reason には含めない（会話ログ経由で露出するため）。
        # 詳細は stderr ログにのみ残す。
        sys.stderr.write(f"[enforce-go-robust-stop] state file corrupted: {path}: {exc}\n")
        raise StateCorrupted("session state JSON is corrupted") from exc
    # json.loads 成功しても schema 違反（list / 数値 / 型ズレ）だと state.get() 等で
    # AttributeError になり fail-closed が機能しない。想定形に適合しない場合は
    # StateCorrupted に倒して明示的な block 経路に乗せる。
    if not isinstance(data, dict):
        sys.stderr.write(
            f"[enforce-go-robust-stop] state not a dict: {path}: type={type(data).__name__}\n"
        )
        raise StateCorrupted("session state is not a dict")
    last_review = data.get("last_review_command")
    if last_review is not None and not isinstance(last_review, dict):
        sys.stderr.write(f"[enforce-go-robust-stop] state.last_review_command not dict: {path}\n")
        raise StateCorrupted("last_review_command is not a dict")
    enforced = data.get("enforced_count")
    if enforced is not None and not isinstance(enforced, int):
        sys.stderr.write(f"[enforce-go-robust-stop] state.enforced_count not int: {path}\n")
        raise StateCorrupted("enforced_count is not an int")
    # last_review_command.ts / last_go_robust を str として検証する。
    # 不正型（list / dict / int 等）だと main() の `last_go_robust >= last_review_ts`
    # 比較で TypeError になり、fail-closed の StateCorrupted 経路に乗らず hook が落ちる。
    if isinstance(last_review, dict):
        ts = last_review.get("ts")
        if ts is not None and not isinstance(ts, str):
            sys.stderr.write(
                f"[enforce-go-robust-stop] state.last_review_command.ts not str: {path}\n"
            )
            raise StateCorrupted("last_review_command.ts is not a str")
    last_go_robust = data.get("last_go_robust")
    if last_go_robust is not None and not isinstance(last_go_robust, str):
        sys.stderr.write(f"[enforce-go-robust-stop] state.last_go_robust not str: {path}\n")
        raise StateCorrupted("last_go_robust is not a str")
    return data


def save_state(session_id: str, state: dict) -> None:
    """state を原子的に保存する（submit hook と同じロジック）。

    submit hook が書いている途中で stop hook が読むと壊れた JSON を拾って
    enforcement が抜けるため、tmp → os.replace() 方式に揃える。
    """
    path = _state_path(session_id)
    if path is None:
        return
    tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def has_pending_markers(text: str) -> bool:
    """
    last_assistant_message に『未処理の要確認』が含まれるかを判定する。

    判定基準:
    - 要確認ヘッダー必須（`## 要確認` / `## Requires confirmation` を `##`〜`####` の
      いずれかで受理。件数括弧付き `## 要確認（N件）` や rfl 集約の `### 要確認（Y件）` も対応）
    - 加えて以下のいずれか:
      - `severity:` 行 + `auto_fixable: false` 行 の両方（IFR Step 4 カード形式）
      - `─────` 区切り（IFR Step 4 フォーマットの区切り線）
      - `- [warning] ...` / `1. [critical] ...` 等のリスト形式 severity マーカー
        （rfl 集約出力・merge_parallel_reviews.py の Markdown 出力で使用）
    """
    if not text:
        return False

    has_header = bool(MARKER_HEADER.search(text))
    if not has_header:
        return False

    has_severity = bool(MARKER_SEVERITY.search(text))
    has_autofix_false = bool(MARKER_AUTOFIX_FALSE.search(text))
    has_divider = bool(MARKER_DIVIDER.search(text))
    has_list_severity = bool(MARKER_LIST_SEVERITY.search(text))

    return (has_severity and has_autofix_false) or has_divider or has_list_severity


def build_block_reason(review_cmd: str) -> str:
    return (
        f"/{review_cmd} が出力した「要確認」がまだ処理されていません。\n"
        "ユーザーに返す前に /go-robust を実行し、"
        "5原則（堅牢性優先・安全側・検知可能・必要十分・AIエージェント可読性）で"
        "判断可能な項目をすべて処理してください。\n\n"
        "今回だけスキップしたい場合は次の入力で /skip-go-robust-once を実行するか、"
        "次回のレビューコマンドに --no-go-robust フラグを付けてください。"
    )


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        # stdin JSON が壊れているとき、pending marker の有無を判定できないため
        # ここで block を返すと任意の壊れ入力で DoS 化する。fail-closed は
        # 「pending が検知されたときに silent pass しない」ことが主眼なので、
        # 入力不能時は stderr に検知可能ログだけ残して pass する（runtime 側調査用）。
        sys.stderr.write(
            f"[enforce-go-robust-stop] stdin JSON parse failed: {exc}; "
            "pending 判定不能のため pass します (runtime 調査対象)\n"
        )
        return 0

    # JSON parse 成功しても top-level が dict 以外（list / 数値 / None）だと
    # 後続 data.get() が例外化する。pending 判定不能扱いで pass + stderr 記録。
    if not isinstance(data, dict):
        sys.stderr.write(
            f"[enforce-go-robust-stop] stdin JSON is not an object: type={type(data).__name__}; "
            "pending 判定不能のため pass します (runtime 調査対象)\n"
        )
        return 0

    # 無限ループ防止（Claude Code built-in）
    if data.get("stop_hook_active"):
        return 0

    # 要確認マーカーが無い通常応答は、session_id 欠落・state 異常であっても
    # block しない（DoS 化を防ぐ）。fail-closed は「pending marker が検知された時に
    # silent pass しない」ことが主眼であり、pending が無い応答まで止める必要はない。
    last_msg_raw = data.get("last_assistant_message")
    last_msg = last_msg_raw if isinstance(last_msg_raw, str) else ""
    has_pending = has_pending_markers(last_msg)

    session_id_raw = data.get("session_id")
    session_id = session_id_raw if isinstance(session_id_raw, str) else ""
    if not session_id:
        if not has_pending:
            return 0
        # session_id なしだと state を引けず enforcement guarantee を保てない。
        # pending marker が検知された場合のみ fail-closed で block する。
        reason = (
            "[enforce-go-robust-stop] session_id missing: state を追跡できないため "
            "enforcement guarantee を保てません。\n"
            "要確認が検知されたため、安全側に倒してブロックします。\n\n"
            "復旧方法:\n"
            "  1. `/go-robust` を手動実行してから再度返答する\n"
            "  2. どうしても今回だけスキップする場合は `/skip-go-robust-once` を実行する"
        )
        output = {"decision": "block", "reason": reason}
        print(json.dumps(output))
        return 0

    try:
        state = load_state(session_id)
    except UnsafeSessionId:
        if not has_pending:
            return 0
        reason = (
            "[enforce-go-robust-stop] unsafe session_id: state を安全に追跡できません。\n"
            "要確認が検知されたため、安全側に倒してブロックします。\n\n"
            "復旧方法:\n"
            "  1. `/go-robust` を手動実行してから再度返答する\n"
            "  2. どうしても今回だけスキップする場合は `/skip-go-robust-once` を実行する"
        )
        output = {"decision": "block", "reason": reason}
        print(json.dumps(output))
        return 0
    except StateCorrupted:
        if not has_pending:
            return 0
        # fail-closed: state 破損は silently pass せず、
        # enforcement guarantee が失われたことを明示してブロックする。
        # ユーザーに復旧導線（state 削除 or /skip-go-robust-once）を提示する。
        # path / session_id は reason に含めない（会話ログ経由で露出するため）。
        reason = (
            "[enforce-go-robust-stop] enforcement guarantee was lost: "
            "session state JSON が破損しています。\n"
            "レビュー後の /go-robust 未実行を検知できない状態になっているため、"
            "安全側に倒してブロックします。\n\n"
            "復旧方法:\n"
            "  1. `/go-robust` を手動実行してから再度返答する\n"
            "  2. どうしても今回だけスキップする場合は `/skip-go-robust-once` を実行する\n"
            "  3. state 自体が恒久的に壊れている場合は該当 state ファイルを削除する"
        )
        output = {"decision": "block", "reason": reason}
        print(json.dumps(output))
        return 0

    if state is None:
        # state なし = レビューコマンド未実行、何もしない
        return 0

    last_review = state.get("last_review_command")
    if not last_review:
        return 0

    last_review_ts = last_review.get("ts", "")
    last_go_robust = state.get("last_go_robust") or ""

    # レビュー後に /go-robust が既に走っていれば通す
    if last_go_robust and last_go_robust >= last_review_ts:
        return 0

    # 脱出口: bypass_once (one-shot) → 消費して通す
    if state.get("bypass_once"):
        state["bypass_once"] = False
        try:
            save_state(session_id, state)
        except Exception as exc:
            # fail-closed: bypass_once 消費の永続化に失敗した場合、
            # 同じ bypass が次回も効いてしまうため enforcement guarantee が弱くなる。
            # pending marker が無ければ通し、ある場合は block に倒す。
            sys.stderr.write(
                f"[enforce-go-robust-stop] save_state failed (bypass_once consume): {exc}\n"
            )
            if not has_pending:
                return 0
            reason = (
                "[enforce-go-robust-stop] state 保存失敗: bypass_once の消費を永続化できませんでした。\n"
                "enforcement guarantee を保てないため、安全側に倒してブロックします。\n\n"
                "復旧方法:\n"
                "  1. `/go-robust` を手動実行してから再度返答する\n"
                "  2. どうしても今回だけスキップする場合は `/skip-go-robust-once` を実行する"
            )
            output = {"decision": "block", "reason": reason}
            print(json.dumps(output))
            return 0
        return 0

    # 要確認マーカーが無い通常応答は block しない。
    # MAX_ENFORCE チェックより先に評価することで、一度 MAX_ENFORCE に達した後も
    # pending を含まない通常応答は通過でき、DoS 化を防ぐ。
    if not has_pending:
        return 0

    # 安全弁: 同一サイクルで MAX_ENFORCE 回ブロック済みの場合。
    # 旧実装は silent pass していたが、fail-closed 原則に合わせて block を継続する。
    # 無限ループ防止は decision:"block" + 明示 bypass 手順の提示で担保する
    # （ユーザーが /skip-go-robust-once または --no-go-robust を実行すれば抜けられる）。
    enforced_count = state.get("enforced_count", 0)
    if enforced_count >= MAX_ENFORCE:
        sys.stderr.write(
            f"[enforce-go-robust-stop] MAX_ENFORCE ({MAX_ENFORCE}) reached for session "
            f"{session_id!r}; continuing to block per fail-closed design\n"
        )
        reason = (
            f"[enforce-go-robust-stop] 自動 block 上限 ({MAX_ENFORCE}) に到達しました。\n"
            "同一レビューサイクルで /go-robust が未実行のまま応答しようとしています。\n"
            "silent pass すると enforcement が抜けるため、明示的な操作が必要です。\n\n"
            "復旧方法:\n"
            "  1. `/go-robust` を実行して要確認を処理する\n"
            "  2. どうしても今回だけスキップする場合は `/skip-go-robust-once` を実行する"
        )
        output = {"decision": "block", "reason": reason}
        print(json.dumps(output))
        return 0

    # ブロック発動
    state["enforced_count"] = enforced_count + 1
    try:
        save_state(session_id, state)
    except Exception as exc:
        # fail-closed: enforced_count の永続化に失敗すると MAX_ENFORCE カウントが
        # 進まず同一ブロックを無限ループさせる可能性がある。block 自体は発動するが、
        # state 異常を明示する reason に切り替えて運用で気づけるようにする。
        sys.stderr.write(
            f"[enforce-go-robust-stop] save_state failed (enforced_count update): {exc}\n"
        )
        reason = (
            "[enforce-go-robust-stop] state 保存失敗: enforced_count の永続化に失敗しました。\n"
            "要確認が検知されたため、安全側に倒してブロックしますが、"
            "同一サイクルで MAX_ENFORCE カウントが進まない可能性があります。\n\n"
            "復旧方法:\n"
            "  1. `/go-robust` を手動実行する\n"
            "  2. state ディレクトリ（~/.claude/state/go-robust-enforce/）の権限/容量を確認する"
        )
        output = {"decision": "block", "reason": reason}
        print(json.dumps(output))
        return 0

    cmd_name = last_review.get("name", "review")
    output = {
        "decision": "block",
        "reason": build_block_reason(cmd_name),
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
