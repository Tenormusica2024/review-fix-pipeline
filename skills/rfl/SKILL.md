---
description: intent-first-reviewでレビュー→自動修正→再レビューのループ。レビューはサブエージェントで実行し、自己レビューバイアスを構造的に排除する。
allowed-tools: Read, Glob, Grep, Edit, Write, Bash(git *), Bash(python*), Bash(node*), Bash(claude*), Bash(*codex*), Bash(*ANTHROPIC_*), Bash(rm *), Bash(cat *), Bash(mktemp*), Bash(command *), Bash(cmd *), Bash(wc *), Bash(ls *), Agent
---

# /review-fix-loop - 高精度レビュー&自動修正ループ

## Mission

**intent-first-reviewベースの高精度レビューをサブエージェントで実行し、Warning以上の問題を自動修正し、クリーンになるまで最大5ループ繰り返す。**

レビューと修正を別コンテキストに分離することで、「自分が書いた修正を自分でレビューする」忖度バイアスを構造的に排除する。

---

## iterative-fixとの違い

| 項目 | iterative-fix | review-fix-loop |
|------|--------------|-----------------|
| レビュー方式 | 同一コンテキスト（自己レビュー） | **サブエージェント（独立レビュー）** |
| レビュー基準 | brutal-review（忖度リスクあり） | **intent-first-review（精度優先）** |
| 件数制限 | brutal-reviewの仕様に依存 | **なし（全件報告）** |
| ループ上限 | 3回 | **5回** |
| 速度 | 速い（同一コンテキスト） | やや遅い（サブエージェント起動コスト） |
| 精度 | 中（自己バイアスあり） | **高（構造的バイアス排除）** |

---

## ループ状態ファイル

`$HOME/.claude/review-loop-state.json` にループ状態を保存する。compact による中断後も resume から再開できる。

```json
{
  "loop": 現在のループ番号,
  "base_rev": "差分取得用のgit commit hash（git未管理時はnull）",
  "session_tmpdir": "/tmp/ifr-review-XXXXXX（mktemp -dで作成。resume時に復元）",
  "target_files": ["対象ファイルのパス一覧"],
  "last_modified_files": [],  // Step 3で修正したファイル一覧（Step 4の再レビュー対象）
  "false_positive_counts": [0, 0, 1, 2],  // ループごとの誤検知数
  "total_finding_counts": [20, 10, 6, 7],  // ループごとの全指摘数
  "pending_confirmations": [],  // 要確認の蓄積リスト（各項目に detected_loops: [N] を含む）
  "status": "running|completed|limit_reached"
}
```

**resume 後の確認:** セッション開始時に state ファイルが存在すれば、ループ番号・対象ファイル・`session_tmpdir` を復元してから再開する。`session_tmpdir` のディレクトリが消失している場合は新規 `mktemp -d` で再作成し、state を更新する。

---

## 実行フロー

### Step 0: 初期化

1. base_rev の決定と対象ファイルの特定:
   - まず `base_rev` を決定する: git管理下なら `git rev-parse HEAD`、git未管理なら `null`
   - 引数がある場合: 指定ファイル/ディレクトリ
   - 引数がない場合（gitリポジトリ内）: `git diff --name-only $base_rev` + `git ls-files --others --exclude-standard` で変更ファイル（tracked + untracked）、なければカレントディレクトリ
   - **git未初期化環境のフォールバック**: `git rev-parse --is-inside-work-tree` がエラーを返す場合、git依存の操作をスキップし、カレントディレクトリ配下の全ファイルを対象とする。Step 4の差分取得は全対象ファイルの再レビューにフォールバックする

2. プロジェクトコンテキストの収集（サブエージェントに渡す情報）:
   - **CLAUDE.md**（存在すれば）の設計方針・コーディング規約
   - **対象ファイル一覧**とそのファイル種別（Code/Doc）
   - **プロジェクトの目的**（git logやディレクトリ名から推定、または引数で指定）

3. `--d` / `--parallel` 排他判定（MODE設定）:
```bash
# --d と --parallel は排他。両方指定時は --d を優先
# ※ 以下は擬似コード。実際にはメインコンテキストが引数を解析して判定する
# （単一変数で2値を同時に判定することはできないため、引数リスト全体をチェック）
HAS_D=false; HAS_PARALLEL=false
for arg in "$@"; do
  [ "$arg" = "--d" ] && HAS_D=true
  [ "$arg" = "--parallel" ] && HAS_PARALLEL=true
done
if [ "$HAS_D" = true ] && [ "$HAS_PARALLEL" = true ]; then
  echo "WARNING: --d と --parallel は排他です。--d を優先します" >&2
  MODE="d"
elif [ "$HAS_D" = true ]; then
  MODE="d"
elif [ "$HAS_PARALLEL" = true ]; then
  MODE="parallel"
else
  MODE=""  # 通常モード（Opus単体）
fi
```

4. `--parallel` / `--d` 時の環境変数事前チェック:
```bash
# --parallel 時: GLM（ZAI_AUTH_TOKEN）+ Codex の両方をチェック
if [ "$MODE" = "parallel" ]; then
  if [ -z "$ZAI_AUTH_TOKEN" ]; then
    echo "ERROR: ZAI_AUTH_TOKEN が未設定。GLM並列レビューを実行できません" >&2
    echo "→ GLMなしで Opus + Codex の2モデルで実行します" >&2
  fi
  CODEX_CMD="${CODEX_PATH:-C:/Users/Tenormusica/.npm-global/codex}"
  if ! command -v "$CODEX_CMD" &>/dev/null && [ ! -f "$CODEX_CMD" ]; then
    echo "ERROR: Codex CLI が見つかりません（CODEX_PATH=${CODEX_PATH:-未設定}）" >&2
    echo "→ Codexなしで Opus + GLM の2モデルで実行します" >&2
  fi
fi
# --d 時: Codex のみチェック（GLMは使用しない）
if [ "$MODE" = "d" ]; then
  CODEX_CMD="${CODEX_PATH:-C:/Users/Tenormusica/.npm-global/codex}"
  if ! command -v "$CODEX_CMD" &>/dev/null && [ ! -f "$CODEX_CMD" ]; then
    echo "ERROR: Codex CLI が見つかりません（CODEX_PATH=${CODEX_PATH:-未設定}）" >&2
    echo "→ Opus Agent単体レビュー + メインコンテキスト修正にフォールバックします" >&2
  fi
fi
```
GLM/Codex のいずれかが利用不可の場合、利用可能なモデルのみで並列実行する（ifr.md 部分失敗時フォールバックと同一）。`--parallel` 時: 全モデル利用不可 → Opus 単体にフォールバック。`--d` 時: Codex利用不可 → Opus Agent単体レビュー + メインコンテキスト修正にフォールバック（ループ自体は中断しない）。

**前提環境**: 並列実行手順のシェルコマンドはすべて **Git Bash（MSYS2）前提**。`mktemp` `/tmp` `cat` `rm` 等のPOSIXコマンドを直接使用する（PowerShell互換は考慮しない）。

5. SESSION_TMPDIR の確定（MODE設定後に実行）:
```bash
# --parallel / --d 時: セッション固有tmpディレクトリを作成
# 通常モード（Opus単体）: SESSION_TMPDIR は不要（null）
if [ "$MODE" = "parallel" ] || [ "$MODE" = "d" ]; then
  SESSION_TMPDIR=$(mktemp -d /tmp/rfl-review-XXXXXX)
else
  SESSION_TMPDIR=""  # 通常モードでは使用しない
fi
```

6. ループ状態ファイルの初期化:
```bash
# resume 後に状態ファイルが存在する場合は読み込んで再開
# 存在しない場合は新規作成
# git管理下の場合
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  base_rev=$(git rev-parse HEAD)  # Step 4の差分取得で使用
else
  base_rev=null  # git未管理: Step 4は全対象ファイルを再レビュー
fi
```
```json
{"loop": 1, "base_rev": "$base_rev", "session_tmpdir": "$SESSION_TMPDIR", "target_files": [...], "last_modified_files": [], "false_positive_counts": [], "total_finding_counts": [], "pending_confirmations": [], "status": "running"}
```

7. Review Feedbackセッション開始:
```bash
python "C:/Users/Tenormusica/.claude/scripts/review-feedback.py" inject --reviewer "review-fix-loop"
```

8. ループ開始通知:
```
⚠️ レビューループを開始します（最大5回）。
対象ファイルをループ完了まで手動編集しないでください。
ループ中のファイル変更は再レビュー時の差分検出に影響します。
```

---

### Step 1: レビュー（サブエージェントで実行）

**Agentツールでサブエージェントを起動し、独立したコンテキストでレビューを実行する。**

#### 並列レビューモード（`--parallel` 引数指定時）

`--parallel` が指定された場合、**`/ifr` の並列レビューモードに委譲する**。
並列実行方法・結果マージルール・注意事項はすべて `/ifr`（`ifr.md`）の「並列レビューモード」セクションに定義されている。

- `/rfl --parallel` → 各ループのStep 1で `/ifr --parallel` 相当の3モデル並列レビューを実行
- `--parallel` なし・`--d` なし → 従来通りAgentツール（Opus）のみで実行

**`--parallel` 時の結果マージ手順:**

3モデルの並列実行完了後、以下の手順で結果をマージする:

1. 各モデルの出力をtempファイルに保存（`$SESSION_TMPDIR` は rfl Step 0 でループ状態ファイルに保存した `session_tmpdir` を使用。メインコンテキスト側で `mktemp -d` により事前作成し、サブエージェントプロンプトにパスを渡す）:
   - Opus Agent出力 → `"$SESSION_TMPDIR"/opus-review.md`（Agentの返却テキストをWrite）
   - GLM Bash出力 → `"$SESSION_TMPDIR"/glm-review.md`（Bashの `>` リダイレクト）
   - Codex Bash出力 → `"$SESSION_TMPDIR"/codex-review.md`（同上）

2. マージスクリプトを実行:
```bash
python "C:/Users/Tenormusica/.claude/scripts/merge_parallel_reviews.py" \
  --opus "$SESSION_TMPDIR"/opus-review.md \
  --glm "$SESSION_TMPDIR"/glm-review.md \
  --codex "$SESSION_TMPDIR"/codex-review.md \
  --format markdown --stats
```

3. マージ結果（Markdown）をStep 2の入力として使用する

**スクリプト失敗時のフォールバック:** エラーの場合、メインコンテキストが3モデル出力を手動でマージする（ifr.mdのフォールバックルールに準拠）。

#### Dual レビューモード（`--d` 引数指定時）

`--d` が指定された場合、**`/ifr` の Dual レビューモードに委譲する**。
Opus 4.6（Agentツール）+ Codex gpt-5.4（codex exec）の**2モデルペアレビュー**を実行する。
実行方法・結果マージルール・注意事項はすべて `/ifr`（`ifr.md`）の「Dual レビューモード」セクションに定義されている。

- `/rfl --d` → 各ループのStep 1で `/ifr --d` 相当の2モデルペアレビューを実行
- `--d` と `--parallel` は排他。両方指定時は `--d` を優先
- **`IFR_MODE=review-only` を環境変数として渡す**（ifr.md の SESSION_TMPDIR クリーンアップスキップ条件。rfl Step 3以降で tmpdir を使用するため）

**`--d` 時の結果マージ手順:**

2モデルの並列実行完了後、以下の手順で結果をマージする:

1. 各モデルの出力をtmpファイルに保存:
   - Opus Agent出力 → `"$SESSION_TMPDIR"/opus-review.md`（Agentの返却テキストをWrite）
   - Codex Bash出力 → `"$SESSION_TMPDIR"/codex-review.md`（Bashの `>` リダイレクト）

2. マージスクリプトを `--input` 可変引数で実行:
```bash
python "C:/Users/Tenormusica/.claude/scripts/merge_parallel_reviews.py" \
  --input opus:"$SESSION_TMPDIR"/opus-review.md \
  --input codex:"$SESSION_TMPDIR"/codex-review.md \
  --format markdown --stats
```

3. マージ結果（Markdown）をStep 2の入力として使用する

**スクリプト失敗時のフォールバック:** `--parallel` と同一（メインコンテキストが手動マージ）。

#### サブエージェントへのプロンプト構成（共通）:
```
あなたはintent-first-reviewのレビュアーです。以下のルールに従ってレビューしてください。

## レビュールール
[intent-first-review ifr.mdの内容をここに展開]

## プロジェクトコンテキスト
- プロジェクト概要: [Step 0で収集した情報]
- 設計意図: [CLAUDE.mdやplanファイルから抽出した設計方針]

## レビュー対象ファイル
[対象ファイルのパス一覧]

## 実行モード
- mode: "review-only"（修正は呼び出し元が担当。IFR Step 5の自動修正ループは実行しない）
- 意図確認（IFR Step 1）はスキップする。上記プロジェクトコンテキストを設計意図として扱うこと

## 指示
1. 全ファイルを丁寧に読み、問題箇所をすべて報告してください（※初回レビュー用。ループ2回目以降はStep 4の追加コンテキストで変更差分のみに限定される）
2. 件数制限なし。見つけた問題はすべて出してください
3. 各指摘に severity（critical / warning / info）を付与してください
4. 出力はifr.md Step 4のフォーマット（Markdown）で返してください（以下は必須項目の要約。正式フォーマットはifr.md Step 4を参照）:
   - 「自動修正可」「要確認」に分類
   - 各指摘に severity・auto_fixable・ファイルパス:行番号・修正内容を含める
```

**毎ループで新しいサブエージェントを起動する**（前回の修正コンテキストを引き継がない）。

---

### Step 2: 分類と提示

サブエージェントから返された指摘をseverityで分類し、ユーザーに提示する。

#### 自動修正可（確認なしで修正）
- `auto_fixable: true` かつ severity が Warning 以上（criticalでもauto_fixable:trueならStep 3で実測確認の上で修正する）
- 表記ゆれ・typo・フォーマット違反
- コメント・ドキュメントのみの修正（ロジック変更なし）
- 同一ファイル内で明確に矛盾している記述

#### 要確認（蓄積してループ完了後に一括提示）
- `auto_fixable: false`（設計判断が必要なもの）
- 設計判断が必要なもの（構造変更・削除・リネーム）
- 「意図的かもしれない」と読める実装や記述
- 削除・大幅書き換えを伴う変更

#### 対象外（Info以下）
- severity が Info 以下の指摘は、auto_fixableの値に関わらずループ判定・修正対象に含めない
- ユーザーへの報告には含める（参考情報として提示フォーマットの「対象外」セクションに記載）

**蓄積ルール:**
- 要確認はループ状態ファイルの `pending_confirmations` に追加し、**ループをブロックしない**。重複排除: `file_path + 行番号(±3行) + タイトル` が既存項目と一致する場合は新規追加せず、`detected_loops` リストにループ番号を追記する
- ループ判定（Warning以上 = 0件）は `auto_fixable: true` の指摘のみで判定する
- **例外: severity が critical かつ auto_fixable が false の要確認はループを中断**し、即座にユーザーに確認する（設計変更を伴う修正を自動で進めるとループ方向がズレるため）。中断時はループ状態ファイルを削除し、`python "C:/Users/Tenormusica/.claude/scripts/review-feedback.py" close-session --reviewer "review-fix-loop" --reason "critical-interrupt"` を実行してからユーザーに報告する
- メインコンテキストが誤検知と判断してスキップした指摘は、severityに関わらず `false_positive_counts` を+1する
- ループ完了後（Step 5 or Step 6）に蓄積した要確認を一括提示する

#### 堅牢方向の自動選択ルール・自律修正原則

**詳細は `/ifr`（`ifr.md`）Step 4 出力フォーマットの blockquote を参照**（SSoT）。
概要: 堅牢な方を自動選択、再検出可能性の高い問題は `auto_fixable: true` として自動修正。

提示フォーマット:
```
## レビュー結果（ループ N/5）

### 自動修正可（X件）
- [severity] [問題の概要] @ [ファイル名:行番号]
...

### 要確認（Y件）
1. [severity] [問題の概要] @ [ファイル名:行番号]
   → 方針: [選択肢A] or [選択肢B] ?
...

### 対象外（Info以下）（Z件）
- [問題の概要]（修正不要・参考情報として記載）
```

**遷移条件（明示的分岐）:**
- `auto_fixable: true` かつ Warning以上 が **> 0件** → 要確認を `pending_confirmations` に蓄積し、**Step 3へ進む**
- `auto_fixable: true` かつ Warning以上 が **= 0件** → **Step 5（完了）へ直行**（要確認は蓄積済みのため完了時に一括提示）
- **例外:** severity: critical かつ auto_fixable: false の要確認が存在する場合はループを中断し、ユーザーに即確認する（蓄積ルールの例外条件と同一）

---

### Step 3: 修正実装

「自動修正可」全件を修正する。要確認はループ状態ファイルに蓄積し、この時点では修正しない。

修正の優先順位: Critical → Warning の順。

**修正ファイルの記録（Step 4の再レビュー対象限定に使用）:**
Step 3で実際に修正したファイルの一覧を記録し、ループ状態ファイルの `last_modified_files` に保存する。Step 4はこの一覧のみを再レビュー対象とする（`base_rev` からの全差分ではなく、直前ループで触ったファイルに限定）。

#### 通常モード（`--d` なし）: メインコンテキストで修正

**修正時の原則:**
- メインコンテキストがプロジェクト全体の設計意図を把握しているため、修正精度が高い
- サブエージェントの指摘をそのまま機械的に適用するのではなく、プロジェクト全体との整合性を確認してから修正する
- 指摘が誤検知だと判断した場合は、修正せずその理由をユーザーに報告する

#### Codex修正モード（`--d` 時）: Codexに修正を委譲

`--d` 指定時は、Step 2で分類した「自動修正可」の指摘をCodexに渡して修正させる。

**修正プロンプトの構成:**
```
あなたはコードの修正担当です。以下のレビュー指摘に基づいて、対象ファイルを修正してください。

## 修正対象の指摘一覧
[Step 2で分類した auto_fixable: true かつ Warning以上の指摘をMarkdownで列挙]

## 対象ファイル
[指摘対象ファイルのパス一覧]

## 修正ルール
- 指摘された箇所のみを修正する。関係ない箇所は変更しない
- 修正の意図がコメントで明確でない場合、簡潔なコメントを追加する
- 対象ファイルを直接編集する（codex exec はワーキングディレクトリ内のファイルを直接変更する）
```

**Codex実行:**
```bash
PROMPT_FILE=$(mktemp "$SESSION_TMPDIR"/codex-fix-prompt-XXXXXX.txt)
cat > "$PROMPT_FILE" << 'PROMPT_EOF'
[上記の修正プロンプト]
PROMPT_EOF
cat "$PROMPT_FILE" | "${CODEX_PATH:-C:/Users/Tenormusica/.npm-global/codex}" exec \
  --dangerously-bypass-approvals-and-sandbox 2>"$SESSION_TMPDIR"/codex-fix-stderr.log
CODEX_FIX_EXIT=$?
[ $CODEX_FIX_EXIT -ne 0 ] && cat "$SESSION_TMPDIR"/codex-fix-stderr.log >&2
rm -f "$PROMPT_FILE"
```

**Codex修正後の確認:**
- メインコンテキストは Codex が修正したファイルの差分を確認する（`git diff` またはファイル内容の比較）
- 明らかに誤った修正（ファイル破壊・無関係な変更）があればrevertし、メインコンテキストが該当箇所のみ手動修正する
- 修正されたファイル一覧を `last_modified_files` に記録する

**Codex修正失敗時のフォールバック:**
Codexがエラー（exit code != 0）またはファイルを一切変更しなかった場合、メインコンテキストが通常モードと同じ方法で修正を実行する。

**Critical指摘の実測確認ルール（必須）:**

`severity: critical` の指摘は、修正実装前に必ず実測確認を行う。

```bash
# Python の挙動に関する指摘は python -c で確認
python -c "[サブエージェントが主張する挙動を再現するコード]"

# JavaScript の挙動に関する指摘は node -e で確認
node -e "[サブエージェントが主張する挙動を再現するコード]"
```

- 実測結果がサブエージェントの主張と一致する → critical として修正する
- 実測結果がサブエージェントの主張と異なる → **誤検知**として修正せず、その旨をユーザーに報告し `false_positive_counts` を+1する

**理由:** ループ後半（Loop 3以降）は誤検知率が上昇する傾向がある。critical であっても誤りが混入するため、機械的な適用は正しいコードを壊すリスクがある。

**自動修正済み項目の事後検証（ループ2以降）:**

再レビュー（Step 4）で「前回自動修正した箇所に新たな問題が発生」と指摘された場合、前回の修正が誤検知ベースだった可能性がある。この場合:
1. 該当修正をrevertし、修正前の状態に戻す
2. 元の指摘を誤検知として `false_positive_counts` に+1する
3. revert理由をユーザーに報告する

---

### Step 4: 再レビュー（新しいサブエージェントで実行）

**直前ループで修正したファイルのみを対象に、新しいサブエージェントでレビューを実行する。**

```bash
# 再レビュー対象: Step 3で記録した last_modified_files を使用
# （base_revからの全差分ではなく、直前ループの修正ファイルに限定）
# git未管理時は last_modified_files をそのまま使用
```

Step 3で `last_modified_files` に記録されたファイル一覧を再レビュー対象とし、Step 1と同じ構造（`--parallel` 時は `/ifr --parallel` と同じ3モデル並列）で新しいサブエージェントを起動する。

**前回のサブエージェントとは完全に独立** — 前回の修正コンテキストを知らないから、忖度が構造的に発生しない。

再レビューのプロンプトには以下を追加:
```
## 追加コンテキスト
これはループN回目の再レビューです。
前回の指摘に対する修正が正しく行われているかの確認と、
修正によって新たに発生した問題がないかの検出が目的です。
`last_modified_files` に含まれるファイルのみを対象とし、各ファイルは全行を丁寧に読んでください。
※初回レビュー指示の「全ファイルを丁寧に読み」は対象ファイル数の制限に置き換わります（対象ファイル内は全行読み）。
```

**ループ状態ファイルを更新:**
```json
{"loop": N+1, ..., "false_positive_counts": [..., 今回の誤検知数], "total_finding_counts": [..., 今回の全指摘数], "last_modified_files": ["Step 3で修正したファイル一覧"]}
```

**判定（優先順位順）:**
```
severity: critical かつ auto_fixable: false が存在
  → ループ中断、ユーザーに即確認（蓄積ルールの例外と同一）

全指摘数 = 0件
  → Step 5（完了）へ直行（クリーン状態）

自動修正可（auto_fixable: true かつ Warning以上）= 0件
  → Step 5（完了）へ（要確認は蓄積済み、完了時に一括提示）

誤検知率 > 50%（= 誤検知数 / 全指摘数 > 0.5）かつ残存の自動修正可（Warning以上）= 0件
  → 実質クリーンとして Step 5（完了）へ
  → 残存指摘はユーザーに「誤検知の可能性が高い」として報告する
  ※ 残存の自動修正可（Warning以上）> 0件の場合は誤検知率に関わらずループ継続

自動修正可 > 0件 かつ ループ回数 < 5
  → Step 2へ戻る

自動修正可 > 0件 かつ ループ回数 = 5
  → Step 6（ループ上限到達）へ
```

**要確認はループ判定に含めない。** 蓄積してループ完了時に一括提示する。

---

### Step 5: 完了処理

**ループ状態ファイルを削除:**
```bash
# Windows
python -c "import pathlib; pathlib.Path.home().joinpath('.claude/review-loop-state.json').unlink(missing_ok=True)"
```

**git管理下の場合（`base_rev` が null でない）:**
ステージング・commit・push・PR 作成は `/commit` に委譲する。
ユーザーへの確認なしに、Claude 自身が続けて `/commit` を実行する。

**git管理外の場合（`base_rev` が null。`.claude/commands/*.md`・`.claude/skills/*.md` 等）:**
commit & push なし。

Review Feedback記録・セッション終了（排他的分岐）:
```bash
# findings を処理した場合、または pending_confirmations が空でない場合（修正して完了）
python "C:/Users/Tenormusica/.claude/scripts/review-feedback.py" record \
  --reviewer "review-fix-loop" \
  --findings '[{"summary":"...","severity":"critical|warning","category":"...","file_path":"...","score":N}]'
# score: 1-5の深刻度スコア（1=軽微, 3=中程度, 5=致命的）。severityをより細粒度で表現する
python "C:/Users/Tenormusica/.claude/scripts/review-feedback.py" close-session \
  --reviewer "review-fix-loop" --reason "completed"

# findings が 0件 かつ pending_confirmations も空で完了した場合（排他: 上記と同時に実行しない）
python "C:/Users/Tenormusica/.claude/scripts/review-feedback.py" close-session \
  --reviewer "review-fix-loop" --reason "no-findings"
```

**1セッション1回だけ close する。** `completed` と `no-findings` は排他的分岐であり、両方実行することはない。
**判定基準:** セッション累積で Warning 以上を 1 件でも処理した場合、または `pending_confirmations` が空でない場合は、最終ループが 0 件でも `record` + `completed` を使う。`no-findings` は「セッション全体を通じて一切の指摘がなかった」場合にのみ使用する。
**pending_confirmationsのみの場合の `--findings` 内容:** 自動修正 findings が 0 件で confirmations のみ蓄積された場合、`--findings` には confirmations を findings として変換して記録する（`severity` はそのまま、`summary` に「要確認: 」プレフィックス付与）。空配列での record は統計上「0件処理」となり実態と乖離するため禁止。

完了報告:
```
## 完了

X回のループでクリーンになりました。
修正内容: [修正した問題の一覧]
```

**蓄積された要確認の一括提示（pending_confirmations が空でない場合）:**
```
## 要確認（ループ中に蓄積された項目）

以下の項目はループ中にレビュアーが検出しましたが、設計判断が必要なため修正せずに蓄積しています。
方針を教えてもらえれば対応します:

1. [severity] [問題の概要] @ [ファイル名:行番号]（ループN検出）
   → 方針: [選択肢A] or [選択肢B] ?
...
```

---

### Step 6: ループ上限到達（5回後も残存問題あり）

**ループ状態ファイルを削除:**
```bash
python -c "import pathlib; pathlib.Path.home().joinpath('.claude/review-loop-state.json').unlink(missing_ok=True)"
```

commitせず、残存問題をそのまま報告する。

Review Feedbackセッション終了:
```bash
python "C:/Users/Tenormusica/.claude/scripts/review-feedback.py" close-session \
  --reviewer "review-fix-loop" --reason "limit-reached"
```

```
## ループ上限到達（5回）

以下の問題が残存しています。手動での判断が必要です:

### 残存 Critical
- [問題] @ [ファイル名:行番号]

### 残存 Warning
- [問題] @ [ファイル名:行番号]

commitは行っていません。
方針を教えてもらえれば、続けて修正します。
```

**蓄積された要確認も同時に提示する（pending_confirmations が空でない場合、Step 5と同じフォーマット）。**

---

## 注意事項

- **レビューは必ずサブエージェントで実行する**: 同一コンテキストでの自己レビューは禁止。これがiterative-fixとの最大の差別化ポイント
- **毎ループで新しいサブエージェントを起動する**: 前回のコンテキストを引き継がないことで忖度を構造的に排除
- **サブエージェントにはプロジェクトコンテキストを十分に渡す**: ifr.md（レビュールール）だけでなく、設計意図・CLAUDE.md・プロジェクト概要を含める。ここが雑だと「一般論」ベースのレビューになる
- **Critical指摘は必ず実測確認してから修正する**: サブエージェントの主張をBash/python -cで検証し、誤検知なら修正しない
- **誤検知率 > 50% は早期終了**: 本物の問題が底をついたサインであり、追加ループは精度を下げるだけになる
- **ループ状態ファイルで中断耐性を確保**: compact 後の resume でもループ番号・対象ファイルを復元して継続できる
- **要確認は蓄積してループ完了後に一括提示**: ループをブロックしない。ただし severity: critical かつ auto_fixable: false の場合のみ即中断してユーザーに確認する。堅牢方向の自動選択ルール・自律修正原則に該当する場合は自動修正してよい（詳細は ifr.md 参照）
- **Info以下はループ対象外**: Warning以上のみをループ判定に使用
- **commitは最後の1回だけ**: 途中ループでのcommitは禁止（差分が追えなくなるため）
- **メインコンテキストは修正の妥当性を判断する権限を持つ**: サブエージェントの指摘が誤検知の場合、修正せずにスキップしてよい（理由をユーザーに報告する）。`--d` 時はCodexが修正を実行するが、メインコンテキストが差分を確認し、明らかに誤った修正はrevertする
- **速度より精度を優先**: サブエージェント起動コストを惜しまない。高精度なレビューのためのトレードオフ
- **`--d` 時のCodex修正フォールバック**: Codex修正失敗（exit code != 0 or 無変更）時はメインコンテキストが通常モードで修正する
- **/commit自動実行は現状維持**: Step 5完了時にユーザー確認なしで `/commit` を自動実行する。git管理下プロジェクトではループ完了→commit→pushまでを一気通貫で行う設計方針（2026-03-30決定）
- **rfl.md は review-fix-loop.md のコピー**: Edit/Write ツールがハードリンク/シンボリックリンクを破壊するため、コピースクリプトで自動同期する。review-fix-loop.md を編集した後は必ず以下を実行:
  ```bash
  python "C:/Users/Tenormusica/.claude/scripts/sync-rfl.py"
  ```
  スクリプトは review-fix-loop.md → rfl.md への内容コピーと整合性検証を自動実行する
