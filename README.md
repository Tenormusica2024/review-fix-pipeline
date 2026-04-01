# review-fix-pipeline

A structured code review pipeline designed to eliminate self-review bias and automate the fix-review loop.

## Overview

This pipeline separates **review** and **fix** into independent contexts, structurally preventing the "I just wrote it, so it looks fine" bias.

```
Code change
    ↓
[ifr] Intent-First Review
    ↓
Auto-fix (auto_fixable findings)
    ↓
[rfl] Independent sub-agent re-review
    ↓
Loop until clean (max 5 iterations)
    ↓
Commit & push
```

## Skills

### `ifr` — Intent-First Review

Reviews code by first inferring the author's intent, then surfacing issues **within that intent** rather than rejecting it outright.

- No sugarcoating — real problems are stated clearly
- No unnecessary negation — design intent is respected
- Findings categorized: `auto_fixable` vs `requires confirmation`
- Unlimited finding count — every issue is reported

**Use when:** You want honest, balanced feedback that respects design decisions while catching real bugs.

### `rfl` — Review-Fix Loop

Runs `ifr` in a **sub-agent** (independent context), applies auto-fixable fixes, then launches a **new sub-agent** for re-review. Repeats up to 5 times.

Key properties:
- Each review loop uses a fresh sub-agent — no context bleed between reviewer and fixer
- `critical` findings are verified empirically (via `python -c` / `node -e`) before applying
- False-positive detection: early exit when false positive rate exceeds 50%
- Supports parallel review modes: `--d` (Opus + Codex) and `--parallel` (3-model)
- Loop state persisted to JSON — survives context compaction and resumes correctly

## Setup (Claude Code)

Copy skills to your Claude Code skills/commands directory:

```bash
# ifr — used as a slash command via /ifr
cp skills/ifr/SKILL.md ~/.claude/skills/ifr/SKILL.md

# rfl — used as a slash command via /rfl
cp skills/rfl/SKILL.md ~/.claude/commands/rfl.md

# Optional: parallel review merge script
cp scripts/merge_parallel_reviews.py ~/.claude/scripts/merge_parallel_reviews.py
```

Then invoke in Claude Code:

```
/ifr                    # review current changes
/ifr --d               # dual review: Opus + Codex
/rfl                    # review-fix loop (up to 5 iterations)
/rfl --d               # dual review mode in loop
```

## Design Principles

**Why sub-agents for review?**
When the same context that wrote the fix also reviews it, the reviewer unconsciously avoids re-detecting its own mistakes. Sub-agents start fresh — they don't know what was changed or why, so they re-detect issues independently.

**Why intent-first?**
Traditional reviews often reject entire design directions without understanding why the author chose them. `ifr` reads intent first, then surfaces issues *within* that intent — more signal, less noise.

**Why empirical verification for critical findings?**
In later loops (loop 3+), false positive rates rise. Blindly applying `critical` fixes can break correct code. Verifying with `python -c` or `node -e` before applying prevents this.

## Requirements

- Claude Code (for skill invocation)
- Python 3.x (for `merge_parallel_reviews.py`)
- Codex CLI (optional, for `--d` mode): `npm install -g @anthropic-ai/codex`

## License

MIT
