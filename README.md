# review-fix-pipeline

> AI agents suffer from the same self-review bias as humans — they unconsciously avoid re-detecting their own mistakes. This pipeline structurally eliminates that bias.

Claude Code skills for **intent-first code review** and **automated fix loops** with independent sub-agent contexts.

---

## The Problem

When an LLM writes a fix and then reviews it in the same context window, it already "knows" why the code looks the way it does. The reviewer and the fixer share the same blind spots.

The standard approach (write → review → fix in one session) produces the same cognitive shortcuts that make human self-review unreliable.

## The Solution

Separate review and fix into **independent contexts**. Each reviewer is a fresh sub-agent with no knowledge of what changed or why — it can only judge what it sees.

```
                    ┌─────────────────────────────────┐
                    │  Code change (diff or files)    │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
                    │  [ifr] Intent-First Review      │  ← Sub-agent A
                    │  Infer intent → find issues     │    (fresh context)
                    └────────────────┬────────────────┘
                                     │
                         ┌───────────▼───────────┐
                         │ auto_fixable findings │
                         │ requires_confirmation │
                         └───────────┬───────────┘
                                     │
                    ┌────────────────▼────────────────┐
                    │  Main context applies fixes     │  ← Fixer
                    │  (critical: empirically tested) │    (separate context)
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
                    │  [rfl] Re-review (new sub-agent)│  ← Sub-agent B
                    │  Modified files only            │    (knows nothing of A)
                    └────────────────┬────────────────┘
                                     │
                         ┌───────────▼───────────┐
                         │  Still auto_fixable?  │
                         │  Yes → loop (max 5)   │
                         │  No  → commit & push  │
                         └───────────────────────┘
```

---

## Skills

### `ifr` — Intent-First Review

Reviews code by **first inferring the author's intent**, then surfacing issues within that intent rather than rejecting it outright.

```
Intent: Minimize latency by caching computed values

## Auto-fixable
### Cache key collision risk
- severity: warning
- auto_fixable: true
- What happens: Two different inputs can produce the same cache key
- Why: str(obj) is not unique for custom objects
- Fix: @ src/cache.py:42 — use hash(frozenset(obj.items())) instead

## Requires confirmation
severity: warning
auto_fixable: false
Issue: Cache is never invalidated
Detail: Stale values accumulate indefinitely; memory grows unbounded over time
Decision point: TTL-based eviction vs explicit invalidation on write?
```

Key behaviors:
- Infers intent before judging — avoids "why didn't you just use X" style feedback
- No finding count limit — every issue is reported
- `auto_fixable: true` = deterministic fix, no design judgment needed
- `requires_confirmation` = design decision required from the author

### `rfl` — Review-Fix Loop

Orchestrates `ifr` as a sub-agent, applies fixes, then re-reviews with a **new** sub-agent. Repeats until clean or 5 iterations.

Key behaviors:
- **Fresh sub-agent each loop** — no context bleed between reviewer and fixer
- **Empirical verification before applying `critical` fixes** — runs `python -c` or `node -e` to confirm the issue actually exists before touching the code
- **False-positive early exit** — when false positive rate exceeds 50%, the loop terminates rather than continuing to degrade
- **Loop state persisted to JSON** — survives context compaction; resumes from the correct loop number
- **Parallel review modes** — `--d` (Opus + Codex dual review) and `--parallel` (3-model consensus)

---

## Setup

```bash
git clone https://github.com/Tenormusica2024/review-fix-pipeline
cd review-fix-pipeline

# Intent-First Review (/ifr)
cp skills/ifr/SKILL.md ~/.claude/skills/ifr/SKILL.md

# Review-Fix Loop (/rfl)
cp skills/rfl/SKILL.md ~/.claude/commands/rfl.md

# Parallel review merge script (required for --d and --parallel modes)
cp scripts/merge_parallel_reviews.py ~/.claude/scripts/merge_parallel_reviews.py
```

Usage in Claude Code:

```
/ifr                 # review current changes (intent-first)
/ifr --d             # dual review: Opus 4.6 + Codex gpt-5.4
/rfl                 # review-fix loop (up to 5 iterations)
/rfl --d             # dual review mode per loop
```

---

## Design Decisions

**Why empirical verification for `critical` findings?**

Loop 3+ shows elevated false positive rates. Applying an incorrect `critical` fix breaks working code and creates new bugs for the next loop to catch — compounding errors rather than eliminating them. Verifying the claimed behavior with a minimal reproduction before applying prevents this failure mode.

**Why persist loop state to JSON?**

LLM context windows compact. Without state persistence, a loop interrupted mid-run loses its position and either restarts from loop 1 (wasting compute) or fails to resume entirely. The state file stores loop number, target files, and `session_tmpdir` path so any session can pick up exactly where the previous one left off.

**Why track false positive counts per loop?**

A rising false positive rate is the signal that the model has run out of real issues and is pattern-matching on noise. Treating that signal as a termination condition — rather than continuing until all 5 loops are exhausted — produces cleaner results and avoids introducing regressions from spurious fixes.

---

## Requirements

- [Claude Code](https://claude.ai/code)
- Python 3.8+
- Codex CLI (optional, for `--d` mode): `npm install -g @openai/codex`

## License

MIT
