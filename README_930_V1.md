# 930.V1 — built on 929.V1

Upload `930.V1.zip` as a new submission. The layout is unchanged.
Everything in 929.V1 is kept (see `README_929_V1.md`), including all the settings behind the larger task's gains.
930.V1 targets what cost the smaller task in the 929.V1 run.

## What the 929.V1 logs show

| | larger task (47 leaves) | smaller task (24 leaves) |
|---|---|---|
| units kept | 33 / 37 | 18 / 22 |
| own tests at the end | 63 / 82 (24 after units; 16 kept repairs) | 12 / 58 (all 8 global repairs reverted) |
| source-generation attempts lost to dropped streams | 17 of 55 | 16 of 35 |
| tokens (prompt / cached / completion) | 6.73M / 2.28M / 2.15M ≈ 33 CNY | 2.15M / 0.93M / 1.12M ≈ 14 CNY |

1. **Dropped connections cost whole units.**
   - The gateway ended long streams mid-answer, usually 2–12 minutes in. Symptoms: `stream chunk could not be decoded`, an empty answer without a finish reason, or `connection reset`.
   - Retries repeated the same long high-effort call and failed the same way. After three attempts the unit was skipped.
   - Units lost: 4 on the larger task (REQ-2-3, REQ-3-1, REQ-3-2-1, REQ-3-2-3). 4 on the smaller task, covering row/column insertion, range selection and copy/cut/paste.
   - One answer cut mid-file was accepted as if it were complete.
2. **The smaller task's own tests measured leftover state, not the app.**
   - All local tests ran in sequence on one server, so each test inherited the previous test's edits.
   - Different requirement groups also describe different contents for the same seeded record. For example, one group's cell values contradict another group's values for the same cells.
   - So most failures pointed at values that were never the app's fault. Repairs that satisfied one group broke another (+2/−5, then +1/−1 four times), and the model twice refused a "fix" as unjustified.
   - The larger task's seed data is consistent across requirements, which is why its repair phase worked.
3. **Test code errors were treated as app failures.** For example, `TypeError: page.getByRole(...).toBeVisible is not a function`.

## Changes (all generic; nothing task-specific)

| Area | Change |
|---|---|
| Dropped streams | A stream that ends without a finish reason counts as cut: complete blocks are kept and the rest is requested as a continuation. |
| Retry after a drop | If a connection drops more than 60 s into an attempt, the retry uses lower effort (high → low → off), so it is shorter and more likely to finish. Early gateway errors keep the effort. |
| Reasoning caps | Lowered to about 45k tokens for code (160k characters). A call still reasoning at the cap is aborted and retried at lower effort. |
| Lost units | Every unit with no usable answer, or that failed verification, is retried once at low effort after all other units, before the repair phase. |
| Test isolation | Local test runs start the host with a reset endpoint. Every local test starts from the freshly seeded data and freshly loaded backend modules. The endpoint does not exist unless the agent's own test runner sets `EVOGIT_TEST_RESET=1`, so the delivered app behaves exactly as before. |
| Seed conflicts | The planning step also returns `seed_conflicts`: which content the seed holds when requirements disagree (the majority content), and which requirements' tests must first enter their own contents through the UI. These go into the implementation, repair and test prompts. |
| Test defects | A test whose own code throws (`TypeError`, `ReferenceError`, `SyntaxError`) is not repaired against. Its file is rewritten once and re-run. |
| Test runner | Clipboard permissions are granted (for paste scenarios). API responses of 400 and above are included in failure reports. |
| Diagnostics | Log failure lines now carry the error's first line with its expected and received values. `MODEL_STATUS.json` stores the failing-test list after the units, at each checkpoint and at the end. The planned seed conflicts are logged. |
| Time budget | `min(16 h, 840 s × leaves + 1 h)`: about 12 h for the larger task and 6.6 h for the smaller. |

**Unchanged for the larger task:** prompts, contracts, reasoning levels for first attempts, context sizes, the repair rules that kept 16 global repairs, and the test helper.

**Cost:**
- The retries after dropped streams, which failed the same way three times in 929.V1, are now cheaper.
- Low-effort retries of lost units add roughly 0.3 CNY per unit.
- `PROFILE = 'balanced'` in `budget_policy.py` still switches to the cheaper V8 settings.

## Local verification
- The offline simulation passes every scenario, plus three new ones: a unit lost to three dropped streams and recovered by the late retry at low effort; a test file with a code error, rewritten and passing; and state isolation (without the reset a second test sees the first test's data, with it the test sees only the seed).
- `python -m unittest discover -s tests` runs 49 tests.
