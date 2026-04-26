# PDCA Bridge Runbook

`review-fix-pipeline` の review 出力を `claude-review-pdca` に流すときの実運用メモ。

## 前提

- sibling repo 構成
  - `C:\Users\Tenormusica\review-fix-pipeline`
  - `C:\Users\Tenormusica\claude-review-pdca`
- もしくは `CLAUDE_REVIEW_PDCA_ROOT` / `PDCA_PRODUCER_PATH` を設定済み

---

## どの bridge を使うか

### 1. `/rfl` 完了後で `review-feedback.py record --findings '[...]'` の JSON がある

**最優先:** `review_feedback_bridge.py`

理由:
- すでに structured findings がある
- markdown 再解析よりノイズが少ない

例:

```bash
python scripts/review_feedback_bridge.py \
  --findings-file /tmp/rfl-findings.json \
  --reviewer review-fix-loop \
  --runtime claude-code \
  --forward-to-pdca \
  --classify-patterns \
  --status fixed
```

### 2. `/ifr` review-only や `/rfl` 後で markdown しか残っていない

`review_output_bridge.py`

例:

```bash
python scripts/review_output_bridge.py \
  --input-file /tmp/review-output.md \
  --reviewer intent-first-review \
  --runtime claude-code \
  --mode review-only \
  --forward-to-pdca \
  --auto-fix-status pending
```

### 3. すでに item 配列を自前で組み立てられる

`review_outcome_contract.py`

例:

```bash
python scripts/review_outcome_contract.py \
  --items-file /tmp/review-items.json \
  --reviewer sc-ifr \
  --runtime codex \
  --forward-to-pdca
```

---

## 推奨フロー

### `/ifr` 単発レビュー

1. 人間向け markdown を出す
2. 可能なら `review-outcome-json` fenced block も併記
3. review-only で終わる場合は `review_output_bridge.py` で PDCA へ流す
4. ただし **`/ifr` の pending はデフォルトで feedback 側を主に使い、pattern 学習は急がない**

### `/rfl` 完了

1. `review-feedback.py record --findings '[...]'`
2. 同じ findings JSON を `review_feedback_bridge.py` にも渡す
3. `--status fixed` を付ける
4. pattern 学習もしたいので `--classify-patterns` を付ける

### `sc-gr` / `/go-robust`

1. `/ifr` や `/rfl` で残った要確認を `sc-gr` / `/go-robust` が処理する
2. **実際にコード修正まで行った resolved item だけ** を PDCA に流す
3. reviewer は `sc-gr` または `go-robust` としてよい（downstream で `go-robust` に正規化される）
4. unresolved のまま残った judgment item は pattern 学習しない

例:

```bash
python scripts/review_outcome_contract.py \
  --items-file /tmp/go-robust-items.json \
  --reviewer sc-gr \
  --runtime codex \
  --forward-to-pdca
```

---

## 自動解決の優先順位

`--forward-to-pdca` は次の順で producer を探す:

1. `--producer-path`
2. `PDCA_PRODUCER_PATH`
3. `--pdca-root`
4. `CLAUDE_REVIEW_PDCA_ROOT`
5. sibling repo の `../claude-review-pdca/scripts/record-review-outcome.py`

---

## 最低限の確認コマンド

### review-feedback bridge

```bash
python scripts/review_feedback_bridge.py \
  --findings-file /tmp/rfl-findings.json \
  --reviewer review-fix-loop \
  --forward-to-pdca \
  --status fixed
```

期待:
- `recorded_patterns >= 1`
- safe fix 済み findings が pattern 側へ流れる

### markdown bridge

```bash
python scripts/review_output_bridge.py \
  --input-file /tmp/review-output.md \
  --reviewer sc-ifr \
  --repo-root C:/path/to/actual-target-repo \
  --forward-to-pdca
```

期待:
- pending finding は feedback 側へ
- fixed / high-confidence なものは pattern 側にも流れる

---

## 運用メモ

- `/rfl` は **structured findings bridge 優先**
- `/ifr review-only` は **markdown bridge** が自然
- `sc-gr` / `/go-robust` は **fixed になった resolved item のみ pattern 学習**するのが自然
- **対象 repo が `review-fix-pipeline` 自身でない場合は `--repo-root` か `--cwd` を必ず明示**する
- machine-readable block が安定したら、markdown parser 依存を徐々に減らしてよい
- false positive 学習は引き続き HITL を維持する
