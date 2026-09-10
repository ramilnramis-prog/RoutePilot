# RoutePilot — workspace instructions

RoutePilot is a route planning and route optimization application for drivers, couriers and
dispatchers. The product principle is: **RoutePilot recommends, the driver decides.** Synthetic
demo data must never be presented as real routing, traffic or opening hours.

## Source of truth — never rely on chat memory

1. `docs/PRODUCT_SPEC_v2.md` — the current product specification.
2. Explicitly approved decisions in `docs/DECISIONS.md`.
3. `docs/ARCHITECTURE.md`.
4. The authorized stage scope, recorded in `docs/DECISIONS.md`.
5. Implementation and tests.

`docs/PRODUCT_SPEC.md` is historical v1 and must never override v2. Specification files are stored
**verbatim** and are never rewritten or summarised. If documents conflict, the current
specification plus explicitly approved later decisions control implementation.

## Authorized work

- Do exactly the authorized work unit you are given. Never begin work from a later stage.
- Only the owner authorizes a stage or a scope change, and only the owner changes product
  semantics. If a unit cannot be completed without a product decision, escalate instead of
  deciding.
- Routine engineering is decided locally: helper naming, obvious test additions, fixing a failing
  test, refactoring, formatting, and file placement already implied by the architecture.

## Roles (see `.dsh/skills/`)

| Role | What it is |
|---|---|
| `supervisor` | the resident session agent: selects work units, runs the review loop, verifies independently, commits, reports, escalates |
| `builder` | a fresh child agent: the only normal writer; implements the unit and reports evidence |
| `reviewer` | a fresh independent child agent: read-only by instruction; returns PASS / REVISE / ESCALATE_TO_OWNER |

Read `docs/WORKFLOW.md` before coordinating or taking part in a work unit.

## Write and commit protocol

- **Builder and Reviewer never commit** and never rewrite git history.
- The **Supervisor** makes one clean commit per accepted work unit, and only after the Reviewer's
  PASS **and** its own independent verification.
- If the Reviewer returns PASS but the Supervisor finds a material problem: do not commit, record a
  **false PASS**, and send the issue through another Builder/Reviewer cycle.
- Never commit secrets, API keys, databases, logs, caches or temporary artifacts.

## Workspace fingerprint (write safety)

```
python tools/workspace_fingerprint.py --repo <repo> [--json]
```

prints a deterministic digest of the working tree: tracked diff, staged diff, every untracked file
path with its content hash, and HEAD. Ignored files are excluded on purpose, because a legitimate
read-only review runs tests and tests create caches.

- A Builder report must carry the fingerprint taken **after** its work.
- A Reviewer must report `workspace_fingerprint_before_review` and
  `workspace_fingerprint_after_review`; they must be equal, and equal to the Builder's fingerprint.
  A PASS whose fingerprints disagree is rejected by the workflow.
- This is protection against an accidental Reviewer write, not a security boundary.

## Verification

- **Builder and Reviewer** run targeted verification for the functionality they touched, plus
  whatever the work unit requires. Do not re-run the entire suite in every review cycle.
- **Supervisor only, after Reviewer PASS** — the authoritative verification:
  `python -m unittest discover -s tests -t .`, `python -m compileall -q core demo tools tests`,
  `python tools/doctor.py`, plus any benchmark or demo command the unit requires.

`doctor` reports a `tzdata` WARN on this machine: there is no working network, so the pure-data
`tzdata` package cannot be installed. Use `PYTHONTZPATH` (or `python -m demo.report
--allow-system-tzdata`). This is an accepted environment limitation, not a product decision, and it
must not weaken the IANA / strict-DST model.

## Recovering state after a restart

Repository governance and git are authoritative; correctness must not depend on session memory or
on any session-goal feature.

1. Read the authorized stage scope in `docs/DECISIONS.md`.
2. `git log --oneline` shows which work units were accepted — each accepted unit is one commit.
3. The next work unit is the first unaccepted item in that stage's change set.
4. A dirty working tree with no PASS record is an unfinished unit: run a fresh review against the
   existing diff instead of rebuilding it.
