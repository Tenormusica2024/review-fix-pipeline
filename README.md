# review-fix-pipeline

> AI agents suffer from the same self-review bias as humans — they unconsciously avoid re-detecting their own mistakes. This pipeline reduces that bias structurally, by separating reviewer and fixer into independent contexts.

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
                         │ ## Auto-fixable       │
                         │ ## Requires confirm.  │
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
- `requires_confirmation` = design decision required from the author (exception: robustness-only tradeoffs with no behavior change are auto-resolved by the fixer)

### `rfl` — Review-Fix Loop

Orchestrates `ifr` as a sub-agent, applies fixes, then re-reviews with a **new** sub-agent. Repeats until clean or 5 iterations.

Key behaviors:
- **Fresh sub-agent each loop** — no context bleed between reviewer and fixer
- **Empirical verification before applying `critical` fixes** — runs `python -c` or `node -e` to confirm the issue actually exists before touching the code
- **False-positive early exit** — when false positive rate exceeds 50%, the loop terminates rather than continuing to degrade
- **Loop state persisted to JSON** — survives context compaction; resumes from the correct loop number
- **Parallel review modes** — `--d` (Opus + Codex dual review) and `--parallel` (3-model consensus)

### `go-robust` — Requires-Confirmation Processor

Applies five principles to every `requires_confirmation` item from a review and resolves whatever can be decided without further input:

1. Robustness / maintainability / stability first
2. On uncertainty, bias toward the safer assumption
3. No silent problems — anything anomalous must be detectable (exception, log, assertion)
4. Keep the change necessary and sufficient — no gratuitous refactors
5. AI-agent readability / long-term maintainability — a fresh-context agent should be able to navigate the file immediately

Runs automatically after `/ifr` and `/rfl` finish. Items that still require human judgment are surfaced; everything else is committed and pushed.

---

## Enforcement Hooks (optional)

To guarantee `/go-robust` runs before review output is returned to the user, install the two hooks in `hooks/`:

- `enforce-go-robust-submit.py` — UserPromptSubmit. Tracks when `/ifr`, `/rfl`, or `/go-robust` is invoked and records the session state in `~/.claude/state/go-robust-enforce/<session_id>.json`.
- `enforce-go-robust-stop.py` — Stop. When the assistant's last message contains `requires_confirmation` markers (`## 要確認` + severity + `auto_fixable: false`, or the `─────` separator) and `/go-robust` has not yet run for the current review cycle, returns `decision: "block"` with a reason so the runtime forces `/go-robust` to execute.

Escape hatches (for the rare case you explicitly want to skip):

- `--no-go-robust` flag on the review command (one-shot bypass for the current cycle)
- `/skip-go-robust-once` command issued after the review output (one-shot bypass for the next response)

Safety caps: `stop_hook_active` short-circuits; each cycle is capped at `MAX_ENFORCE = 2` blocks; `bypass_once` is consumed after a single use.

---

## Setup

> **Prerequisite:** The setup commands and `rfl` shell examples require **Git Bash** (Windows) or a POSIX shell (macOS/Linux). PowerShell is not supported.

```bash
git clone https://github.com/Tenormusica2024/review-fix-pipeline
cd review-fix-pipeline

# Intent-First Review (/ifr)
mkdir -p ~/.claude/skills/ifr
cp skills/ifr/SKILL.md ~/.claude/skills/ifr/SKILL.md

# Review-Fix Loop (/rfl)
mkdir -p ~/.claude/skills/rfl
cp skills/rfl/SKILL.md ~/.claude/skills/rfl/SKILL.md

# Requires-Confirmation Processor (/go-robust)
mkdir -p ~/.claude/skills/go-robust
cp skills/go-robust/SKILL.md ~/.claude/skills/go-robust/SKILL.md

# Scripts (required for all modes)
mkdir -p ~/.claude/scripts
cp scripts/merge_parallel_reviews.py ~/.claude/scripts/merge_parallel_reviews.py
cp scripts/review-feedback.py ~/.claude/scripts/review-feedback.py

# Enforcement hooks (optional — see "Enforcement Hooks" section above)
mkdir -p ~/.claude/hooks
cp hooks/enforce-go-robust-submit.py ~/.claude/hooks/enforce-go-robust-submit.py
cp hooks/enforce-go-robust-stop.py ~/.claude/hooks/enforce-go-robust-stop.py
```

After copying the hook scripts, register them in `~/.claude/settings.json` under `hooks.UserPromptSubmit` and `hooks.Stop` so Claude Code invokes them. See the Claude Code hooks documentation for the exact schema.

Usage in Claude Code:

```
/ifr                 # review + auto-fix current changes (intent-first)
/ifr --d             # dual review: Opus 4.6 + Codex gpt-5.4
/ifr --c             # dual Codex review: 2 instances with split angles (quality vs security)
/ifr --parallel      # 3-model consensus: Opus + Codex + GLM (requires ZAI_AUTH_TOKEN)
/rfl                 # review-fix loop (up to 5 iterations)
/rfl --d             # dual review mode per loop
/rfl --c             # dual Codex review mode per loop
/rfl --parallel      # 3-model consensus per loop
/go-robust           # process accumulated requires_confirmation items using the 5 principles
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
- Python 3.10+
- Codex CLI (optional, for `--d` mode): `npm install -g @openai/codex`

## License

MIT
