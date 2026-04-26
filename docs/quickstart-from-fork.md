# Quickstart from Fork

この文書は、`review-fix-pipeline` と `claude-review-pdca` を **fork / clone して最短で流れを再現するための骨組み**。

まだ完全自動 bootstrap ではないが、**どこで詰まりやすいか** と **最短の golden path** を先に固定する。

---

## 目標

まずは次の 1 本だけ再現できればよい。

1. `review-fix-pipeline` で review output を作る
2. bridge で `claude-review-pdca` に forward する
3. `review-feedback.db` または `review-patterns.db` に記録される

この quickstart は **fork-ready 完成版** ではなく、
**fork 後の最短確認ルート** を共有するための下地。

---

## 推奨ディレクトリ構成

```text
<workspace>/
  review-fix-pipeline/
  claude-review-pdca/
```

この sibling repo 構成がいちばん分かりやすい。

---

## 前提

- Python 3.10+
- Git
- 2 repo を clone 済み
- 必要なら `CLAUDE_REVIEW_PDCA_ROOT` を設定

Windows 例:

```powershell
$env:CLAUDE_REVIEW_PDCA_ROOT = "C:\path\to\claude-review-pdca"
```

---

## Golden path

### 1. `review-fix-pipeline` へ移動

```powershell
cd C:\path\to\review-fix-pipeline
```

### 1.5 対象 repo を 1 つ決める

この quickstart では、bridge 元 repo とは別に

```text
C:\path\to\sample-target-repo
```

のような **actual target repo** がある前提で進める。

`review-fix-pipeline` 自身を target repo にしても動作確認はできるが、
本来の使い方は **別repoへの review memory / PDCA 連携**。

### 2. 最小の review markdown を用意

例:

```markdown
## Auto-fixable
### shell quoted subprocess is fragile
- Severity: warning
- Target: scripts/review_output_bridge.py:55
- What happens: quoted shell execution can break on special characters
```

### 3. unified runner から PDCA に流す

```powershell
python scripts/pdca_bridge_runner.py `
  --kind output `
  --input-file C:\tmp\review-output.md `
  --reviewer sc-ifr `
  --runtime codex `
  --mode review-only `
  --repo-root C:/path/to/sample-target-repo `
  --forward-to-pdca
```

期待:
- `recorded_feedback >= 1`
or
- `recorded_patterns >= 1`

---

## 詰まりやすい点

### 1. `--repo-root` を省略する

cross-repo forward 時にもっとも壊れやすい。

**必ず actual target repo を `--repo-root` で明示**する。
bridge を実行している repo (`review-fix-pipeline`) 自身を何となく入れないこと。

### 2. sibling repo ではない

その場合は:

- `CLAUDE_REVIEW_PDCA_ROOT`
or
- `PDCA_PRODUCER_PATH`

を設定する。

### 3. legacy markdown の field 名が揺れる

現在は少なくとも次を受け付ける:

- `Severity` / `severity`
- `Target` / `対象`
- `What happens` / `何が起きるか`
- `Issue` / `問題`
- `Detail` / `詳細`
- `Decision point` / `判断ポイント`

---

## 次の段階

この quickstart が通ったら、次に見るのは:

1. `/ifr review-only`
2. `/rfl fixed findings`
3. `sc-gr`

の 3 経路。

詳細は `docs/pdca-bridge-runbook.md` を参照。

---

## 今後の予定

将来的にはここに追加したい:

- bootstrap script の本格化
- hook / env / DB 初期化の自動化
- sample workspace
- machine-readable block 前提のより短い quickstart
