# review-fix-pipeline 公開向け説明メモ

作成日: 2026-04-30
対象 repo: `Tenormusica2024/review-fix-pipeline`

## 一言でいうと

AIが自分で書いたコードを同じ文脈で甘くレビューしてしまう問題を避けるため、レビュー担当と修正担当を分離するレビュー・修正ループ基盤です。

## クライアント向けの説明粒度

### 1. 何の課題を解くものか

AIコーディングでは、同じセッション内で「実装したAI」がそのまま「レビューするAI」になると、実装意図を知っているぶん問題を見落としやすくなります。

`review-fix-pipeline` は、この自己レビューの甘さを構造的に減らすため、レビュー・修正・再レビューを分けて実行するための workflow / skill 群です。

### 2. 具体的に何をしているか

- intent-first に、まず変更意図を推定してからレビューする
- safe fix と要確認事項を分ける
- 修正後に新しい文脈で再レビューする
- `/ifr`、`/rfl`、`/go-robust` という Claude Code 用 skill を正本として管理する
- Codex 側の `sc-ifr`、`sc-rfl`、`sc-ir`、`sc-gr` と同じ意味論で扱える review outcome contract を設計する
- `claude-review-pdca` へ review outcome を渡し、レビュー記憶・再注入へつなげられるようにする

### 3. 普通のlintやCIとの違い

lintやCIは、形式違反・テスト失敗・静的に検知できる問題を拾うのが得意です。

この repo が扱うのは、設計意図の取り違え、レビュー観点の漏れ、safe fix / 要確認の切り分け、AIエージェントの自己レビュー偏りといった、通常のCIだけでは拾いにくい領域です。

### 4. claude-review-pdca との関係

- `review-fix-pipeline`: レビューの意味論・分類・workflowの正本
- `claude-review-pdca`: レビュー結果の保存・再注入・学習の基盤

つまり、`review-fix-pipeline` が「レビューをどう判断するか」を担当し、`claude-review-pdca` が「そのレビュー結果をどう記憶して次回に活かすか」を担当します。

### 5. 使う場面

- AI実装後のレビュー品質を安定させたい
- 実装AIとレビューAIの文脈を分けたい
- safe fix だけを自動で進め、判断が必要なものは人間に残したい
- Claude Code と Codex の両方でレビュー結果の意味を揃えたい
- レビュー結果を後続のPDCA基盤へ渡したい

## 30秒説明

このプロジェクトは、AIが自分の実装を同じ文脈で甘くレビューしてしまう問題を避けるためのレビュー・修正ループ基盤です。まず変更意図を推定してレビューし、safe fix と要確認事項を分け、修正後は別文脈で再レビューします。Claude Code の `/ifr` `/rfl` `/go-robust` を正本として管理しつつ、Codex側の `sc-ifr` `sc-rfl` などとも同じ review outcome contract で接続できるように設計しています。単なるlintではなく、AIエージェントのレビュー品質を運用として安定させる仕組みです。

## ポートフォリオでの見せ方

### タイトル案

- AI Review-Fix Loop 基盤
- AI自己レビュー偏りを減らすレビュー分離システム
- Intent-First Review / Review-Fix Pipeline

### 短い説明文案

AI実装後の自己レビュー偏りを減らすため、レビュー担当と修正担当を分離し、safe fix・要確認・再レビューを一連のworkflowとして扱う基盤。Claude Code / Codex 間でレビュー結果の意味論を揃え、PDCA型のレビュー記憶へ接続できる。

### 強調できる技術要素

- Claude Code skill の source of truth 管理
- `/ifr`、`/rfl`、`/go-robust` のworkflow設計
- intent-first review
- safe fix / unresolved / judgment call の分類
- reviewer / runtime をまたぐ review outcome contract
- Codex skill (`sc-ifr`, `sc-rfl`, `sc-ir`, `sc-gr`) との意味論整合
- `claude-review-pdca` へのproducer連携

## 公開時の注意

公開説明では、skill設計やworkflowの概要は出して問題ありません。一方で、実際のレビュー対象repo、未公開コードの指摘、ローカルDB、ローカルパス、秘密情報を含むreview payloadは公開しないでください。公開対象は、上記のような抽象化した仕組み・設計意図・運用思想に限定するのが安全です。
