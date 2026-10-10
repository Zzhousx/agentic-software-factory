# 935.V1 — built on v13 (932.V1): staged-task memory, reliable planning

Upload `935.V1.zip` as a new submission. The layout and the working pattern of v13 (932.V1) are unchanged: one cached prompt prefix, implement → test → review per unit, product conventions, cost-based budgets. 933/934 changes are **not** included (no per-session state isolation, no unit packing).

## What the latest logs (v13 on the new requirement set) showed

| task | units | own tests | est. cost | planning |
|---|---|---|---|---|
| GitHub (full) | 26/26 | 98/100 | 19.9 CNY | **timed out 3×, generic fallback plan** |
| Sheet | 22/22 | 76/100 | 21.9 CNY | ok (after 1 time-out) |
| GitHub stage 1 | 8/8 | 30/30 | 4.0 CNY | ok |
| GitHub stage 2 (inherited stage 1) | 8/8 | 24/29 | 6.3 CNY | **timed out 3×, generic fallback plan** |
| GitHub stage 3 | 10/10 | 40/41 | 7.4 CNY | ok (after 2 time-outs) |

1. **Planning failed in 2 of 5 runs.** The architecture call ran at high effort with the default 300 s attempt cap. Accepted plans took 163–214 s, but 9 of 12 attempts were cut at 300 s, and every retry used the same effort and cap. In those runs the product conventions and seed-conflict notes were missing from every prompt, and 15 minutes were lost.
2. **Stage 2 was planned as if starting from scratch.** The planning prompt did not show the inherited stage-1 application. Nothing checked stage-1 behaviour after the changes either. Stage 1's own acceptance specs were stored in `.arc/`, which the runtime's `.gitignore` leaves out of the project snapshot. In stage 2, 5 of its own tests could not find a "Sign in" control.
3. Staged requirements refer to earlier stages by id ("builds on REQ-1-1-2 (Sign In with an Existing Account)"; stage 3 also depends on REQ-3-3 and REQ-4-3). When a stage starts without an inherited app (stage 3 ran in parallel with stage 2), these prerequisites were never named to the model.

## Changes

### 1. Planning that completes
- The architecture call now gets 600 s per attempt and 1500 s in total.
- An attempt that hits its time cap is retried at a lower effort. This applies to every stage, the same way it already worked for runaway reasoning.
- Failed attempts now log how far they got (reasoning and answer sizes only, no content).

### 2. Project memory for staged tasks (`evogit/`, committed with the sources)
- `acceptance/` holds this stage's specs. `legacy/` holds the specs of all earlier stages. `history/` holds earlier requirement trees, and `plan.json` holds the last plan.
- On an inherited project, the earlier specs are run once against the inherited app. Spec files with passing tests become **regression guards**:
  - they run in every unit test run, review and repair check, and in the full runs;
  - a change that breaks them counts as a regression, just like breaking this stage's own passing tests;
  - regressions caused by a unit are repaired right after that unit;
  - in the final repair phase, failures of this stage's own tests are handled first.
- Tests that already failed on the inherited app are not repair targets.
- The planning step for an inherited project now sees the existing app: its file list, routes, API paths and data keys, the earlier plan, and the earlier requirement list. It is told to extend the app and keep its routes, names, seed records and sign-in flow.
- Earlier-stage prerequisites referenced by the new stage are listed in the plan prompt and in the shared prompt prefix:
  - **inherited:** "keep them working as they are";
  - **no inherited app:** "the application must still provide them".
- On an inherited project, a unit the existing app already satisfies counts as done, even when it is the first unit.

### 3. Cheaper retries after a reverted change
A reverted repair now restores the original file modification times. The source order in the prompt (oldest file first) stays the same, so the provider's cached prefix still applies after a revert.

## What does not change
- Prompts for code generation, tests, reviews and repairs are the same for non-staged tasks, apart from the planning time cap. The cost profile should match v13 (≈20–22 CNY for the large tasks, 4–8 CNY for the stages).
- Staged tasks add one baseline test run plus the guard specs in each test run. This costs test time, not model tokens. Model cost rises only when a guard actually catches a regression.

## Local verification
- `python -m unittest discover -s tests`: 65 tests, 7 of them new (stage memory, guards, time-cap retry, planning prompt, mtime restore).
- Offline simulation, full v13 matrix: unchanged results.
- New two-stage simulation: stage 2 inherits the stage-1 project and its unit breaks a stage-1 control.
  - **v13:** keeps the regression (platform tests 2/4).
  - **935:** the guard detects 2 regressions and the repair restores the control (4/4).
