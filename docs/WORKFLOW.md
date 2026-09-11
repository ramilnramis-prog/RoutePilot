# RoutePilot — development workflow

Purpose: remove the manual copy/paste review loop. The owner authorizes a stage once and then only
receives genuine product questions and accepted-unit summaries — never routine correction cycles.

This protocol uses **native DeepSeek Harness features only**: one resident agent, the `workflow`
tool, subagents, and the repository's own governance files. There is no custom orchestration
framework, no external service, and no second model provider.

---

## 1. Roles

| Role | What it is | May write? | May commit? |
|---|---|---|---|
| **Supervisor** | the resident session agent (`.dsh/skills/supervisor/SKILL.md`) | yes (verification only) | **yes**, after both gates pass |
| **Builder** | a fresh child agent started by the workflow (`.dsh/skills/builder/SKILL.md`) | yes — the only normal writer | **never** |
| **Reviewer** | a fresh independent child agent (`.dsh/skills/reviewer/SKILL.md`) | no — read-only by instruction | **never** |

The Reviewer is a fresh context, never a forked one: forking would leak the Builder's reasoning into
the review and destroy its independence.

---

## 2. Flow

```
owner: "execute <stage>"                     (said once)
   │
SUPERVISOR ── selects the first unaccepted unit of the authorized stage
   │        ── snapshots HEAD, git status and the workspace fingerprint
   │
   └── one `workflow` call: .dsh/workflows/stage-work-unit.js
          args = {unit_id, brief, reviewer_brief, repo}
          │
          ├─ BUILDER  (fresh child) ── implements, runs targeted verification,
          │                            returns the structured report + fingerprint
          │
          ├─ REVIEWER (fresh child) ── fingerprint before → reads governance and the diff,
          │                            re-runs targeted tests, adversarial checks → fingerprint
          │                            after → returns PASS / REVISE / ESCALATE_TO_OWNER
          │
          ├─ REVISE  → the five issue fields go straight back into the next BUILDER prompt
          │             (bounded: at most 3 Builder and 3 Reviewer attempts)
          ├─ ESCALATE_TO_OWNER → returned to the Supervisor, who asks the owner
          └─ PASS    → returned to the Supervisor
   │
SUPERVISOR ── independent verification: git status/diff, fingerprint, full test suite,
   │          compileall, doctor, benchmark/demo
   ├── both gates pass  → one clean commit → increment report to the owner
   └── Reviewer PASS but a material problem found → **no commit**, recorded as a false PASS,
       issue sent through another cycle
```

Nothing about the loop requires the owner to copy messages between agents.

---

## 3. Contracts

### Builder report (structured, schema-enforced)

`files_changed`, `implementation_summary`, `governing_spec_decisions`, `verification_commands`,
`verification_results`, `benchmarks`, `blockers`, `workspace_fingerprint`, `git_status`, `notes`.

The Builder never commits, never works beyond the authorized unit, and reports real command results —
a failure appears as a failure.

### Reviewer verdict (structured, schema-enforced)

Exactly one `verdict`: `PASS`, `REVISE` or `ESCALATE_TO_OWNER`, plus
`workspace_fingerprint_before_review`, `workspace_fingerprint_after_review`, `issues`, `escalation`,
`notes`.

For `REVISE`, one entry per issue with `ISSUE`, `EVIDENCE`, `GOVERNING_SPEC_OR_DECISION`,
`REQUIRED_FIX`, `REQUIRED_VERIFICATION`.

For `ESCALATE_TO_OWNER`, exactly `QUESTION`, `OPTIONS`, `RECOMMENDATION`,
`WHY_THIS_REQUIRES_OWNER_JUDGMENT`, `WHAT_DEPENDS_ON_THE_ANSWER`.

The Reviewer's 13-point checklist (specification, decisions, stage scope, invariants, semantic
drift, assumptions, missing tests, determinism, fake capability, coupling, benchmark evidence,
documentation drift, historical provenance) lives in its skill.

---

## 4. Loop limits

| Limit | Value |
|---|---|
| `MAX_REVISE_CYCLES` | 3 (at most 3 Builder attempts and 3 Reviewer attempts per unit) |
| Registry of attempts | every cycle's report, verdict and fingerprint check is returned |
| Unexpected child failure | a `null` or throwing child counts as a **failed cycle** and is recorded in `notes`; no retry storm |

The workflow's own bound is the cost control. The harness's global `maxTotalAgents` default (1000) is
a runaway backstop only, and is never relied on.

Returned statuses: `PASS`, `REVISE_EXHAUSTED`, `ESCALATE_TO_OWNER`, `INTEGRITY_FAILURE`,
and (implicitly, all cycles failing) `REVISE_EXHAUSTED` with cycle notes.

---

## 5. Write safety: the workspace fingerprint

`git status` alone cannot detect a Reviewer write, because the Builder has already produced a
legitimately dirty tree. Every review phase is therefore bracketed by a deterministic fingerprint:

```
python tools/workspace_fingerprint.py --repo <repo>        # 64-hex digest
python tools/workspace_fingerprint.py --repo <repo> --json # components + digest
```

The digest covers:

| Component | Source |
|---|---|
| HEAD commit | `git rev-parse HEAD` |
| tracked, unstaged changes | `git diff --binary --no-color` (SHA-256) |
| staged changes | `git diff --cached --binary --no-color` (SHA-256) |
| untracked files | every path with size and content SHA-256, sorted |

**Ignored files are excluded on purpose** (`git status` omits them): a read-only review legitimately
runs tests, and tests create caches such as `__pycache__/` or `_scan_scratch/`. Including them would
make the fingerprint unstable for correct behaviour.

The workflow refuses to accept `PASS` when:

* `workspace_fingerprint_before_review != workspace_fingerprint_after_review` (the Reviewer changed
  the tree), or
* the Builder's `workspace_fingerprint != workspace_fingerprint_before_review` (the tree changed
  between the report and the review).

Such a run returns `INTEGRITY_FAILURE` instead of `PASS`. This protects against an accidental
Reviewer write; it is **not** a security boundary, and no second worktree is created.

---

## 6. Test and cost policy

| Phase | Verification |
|---|---|
| Builder | targeted tests for the changed functionality, plus whatever the unit requires |
| Reviewer | independently re-runs the relevant/targeted tests; adds adversarial checks when decisive |
| Supervisor, after Reviewer PASS | **authoritative**: `python -m unittest discover -s tests -t .`, `python -m compileall -q core demo tools tests`, `python tools/doctor.py`, and any benchmark or demo command the unit requires |

The full suite is not re-run in every review cycle — that keeps independent review without pointless
model and tool overhead.

---

## 7. Commit policy

* Builder and Reviewer never commit.
* The Supervisor commits **one clean commit per accepted work unit**, and only after
  Reviewer PASS **and** Supervisor verification PASS.
* A Reviewer PASS combined with a material problem found by the Supervisor is a **false PASS**: no
  commit, one row in the log below, and another Builder/Reviewer cycle.

### Reviewer quality log

| Date | Unit | What the Reviewer missed | How it was found |
|---|---|---|---|
| — | — | *(no false PASS recorded yet)* | — |

### Owner-approved gate exceptions

One row per time the normal gate (Reviewer PASS **and** Supervisor verification before a commit) was
deliberately not applied, with the owner's explicit approval. These are **not** false PASSes: the
Reviewer's verdict was honest in every one of them.

| Date | Unit | Gate not applied | Why, and with whose approval | What the Supervisor did instead |
|---|---|---|---|---|
| 2026-09-11 | U5 (demo narrative and docs) | no second independent review of the final text-only fix | The final fix cycle returned REVISE on two stale demo-plan count labels (`tools/benchmark_optimizer.py`) after the substantive review of the same tree had already passed; the Stage 2 child-call ceiling was spent, and the owner raised it from 36 to **37 for exactly one Builder call and no further Reviewer call**. | Read the diff, then re-ran the authoritative verification on the frozen tree: `unittest discover` (449 tests, OK, 5 skipped), `compileall`, `doctor` (OK with the accepted `tzdata` warning), `demo.report`, `tools/benchmark_optimizer.py` (`ACCEPTED_BOUND_MET=true`) and the opt-in slow quality comparison (31 tests, OK). The commit message and this row record the missing step; the two label texts themselves were independently reviewed in the preceding cycle as defects, so only their **fix** lacks a review. |

---

## 8. Owner communication

The owner is not a message relay. Only two things reach the owner:

1. `ESCALATE_TO_OWNER` decisions (a genuine product decision or genuinely conflicting approved
   requirements);
2. accepted work-unit / stage summaries after both gates pass.

Routine engineering questions — naming, obvious tests, a failing test, refactoring, formatting, file
placement, or "should we follow the specification" — are answered inside the loop and must never be
escalated.

---

## 9. Recovery after a process restart

Repository governance and git are authoritative. Correctness never depends on session memory or on
the native session-goal feature (which, if used, is auxiliary only).

1. Read the authorized stage scope in `docs/DECISIONS.md`.
2. `git log --oneline` shows the accepted units — one commit each.
3. The next unit is the first unaccepted item in that stage's change set.
4. A dirty tree with no PASS record is an unfinished unit: run a fresh review against the existing
   diff instead of rebuilding it.

---

## 10. Workflow files

| File | Role |
|---|---|
| `AGENTS.md` | workspace instructions, injected into every session and every child |
| `.dsh/skills/supervisor/SKILL.md` | Supervisor playbook |
| `.dsh/skills/builder/SKILL.md` | Builder playbook and report contract |
| `.dsh/skills/reviewer/SKILL.md` | Reviewer playbook, checklist and verdict contract |
| `.dsh/workflows/stage-work-unit.js` | the workflow script template the Supervisor passes verbatim |
| `tools/workspace_fingerprint.py` | deterministic working-tree fingerprint |
| `docs/WORKFLOW.md` | this protocol |

`AGENTS.md` and the skills are discovered from the repository with no harness configuration change:
the workspace-instruction plugin injects `AGENTS.md`, and the filesystem skill provider scans
`.dsh/skills/`. The Harness does **not** execute `.dsh/workflows/*.js`: the `workflow` tool takes the
script inline, so that file is a versioned source of truth the Supervisor reads and passes on.

---

## 11. Workflow self-verification (pilot)

Run on 2026-09-11 against the workflow infrastructure itself, **before any Stage 2 product work**, on
a throwaway fixture that was deleted afterwards. Nothing in the pilot touched RoutePilot product
semantics and no Stage 2 code was written.

### Pilot A — the full loop (`PILOT-1`), 4 child agents

| Cycle | Builder | Reviewer | Builder fingerprint | Reviewer before | Reviewer after | Gate |
|---|---|---|---|---|---|---|
| 1 | created the fixture files | **REVISE** (2 issues) | `f357c5e5…e88698` | `f357c5e5…e88698` | `f357c5e5…e88698` | `builder == before`, `before == after` |
| 2 | applied both `required_fix` items | **PASS** | `6daf1613…4f4eab` | `6daf1613…4f4eab` | `6daf1613…4f4eab` | `builder == before`, `before == after` |

The fixture deliberately desynchronised the Builder's brief from the governing contract (the brief
asked for subtraction, the contract required addition), so a real, objective violation existed in the
tree. Both children noticed the conflict and correctly routed it as a Supervisor-level wording
reconciliation rather than an owner escalation.

What the Reviewer did beyond reading the Builder's report: it re-ran the targeted tests itself, called
the fixture module directly (`add(2,2)` → `0`, `add(-1,1)` → `-2` against the required `4`, `0`), and
identified that the delivered test was *circular* — it locked in the wrong behaviour, which made the
verification gate vacuous. On the fix cycle it proved the gate was no longer vacuous by rebinding
`add` to a subtractive implementation in memory and showing that the same two tests then fail.

Supervisor verification after PASS: read both files, re-ran the fixture tests (`Ran 2 tests … OK`),
called the module directly (`4`, `0`, `7`), deleted every pilot artifact, and confirmed the working
tree held only the workflow files.

### Pilot B — the escalation path (`PILOT-ESCALATE`), 2 child agents

A synthetic contract asked the Reviewer to return `ESCALATE_TO_OWNER` without any compliance review.
The workflow returned `status: ESCALATE_TO_OWNER` with all five fields populated (`question`,
`options`, `recommendation`, `why_this_requires_owner_judgment`, `what_depends_on_the_answer`), and
the Reviewer explicitly marked the payload synthetic and stated it must not be relayed to the owner as
a real question. Fingerprints: builder == before == after == `6daf1613…4f4eab`; no repository change
occurred.

### Cost observed in the pilot

| Pilot | Child model calls |
|---|---|
| A — full loop with one REVISE cycle | 4 (2 Builder, 2 Reviewer) |
| B — escalation path | 2 (1 Builder, 1 Reviewer) |
| **Total for workflow verification** | **6** |

`MAX_REVISE_CYCLES = 3` bounds a work unit at 6 child calls. The pilots stayed inside the bound, and
no `INTEGRITY_FAILURE`, `CHILD_FAILURE` or `REVISE_EXHAUSTED` path was triggered.
