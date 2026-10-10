# 929.V1 — built on V8 (V8: 25.5% pass, 34.8 CNY, score 22.91)

Upload `929.V1.zip` as a new submission. Entry point and layout are unchanged (`main.py`, `arcbench-agent-runtime/`, `template/`).
V8's unit adaptation is kept: density-aware unit splitting, dependency focus, the implementation-surface summary and dependency regression scope.

## How the score behaves

All three official results fit one formula, where p is the overall pass rate and c the total cost in CNY:

    score = 100 · p · (80 · p / c) ^ 0.2        (926: 22.38, V8: 22.91, 927: 28.35, each exact to 0.01)

The reference cost is about 80 CNY × pass rate, and cost enters only with exponent 0.2.
Raising the pass rate by 10% is worth about a 60% cost increase.
V8 saved 17 CNY against 927.V1, mainly by lowering reasoning for code generation to `low`. Pass rate fell from 32.5% to 25.5%, with the smaller (spreadsheet-style) task dropping from 55% to 33%, so the score went down.

## Why V8 scored low (from the two logs and the downloaded workspaces)

1. **Our own tests reported false failures on the smaller task.**
   - The test helper `h.click(page, 'A1')` only looked at button, link, tab and similar roles. The generated grid was correct (`role="gridcell"`, `aria-label="A1"`), but the helper could not find it.
   - Result: 21 of 53 local failures were `Not found: control A1`. Repairs chased a bug that did not exist, every one ended `no_net_gain`, and the real failures never got a repair.
   - Reproduced in Chromium: the V8 helper fails on a correct grid, the 929 helper passes.
2. **Shared contracts never reached most prompts.**
   - Rules written once in a module description apply to every module, for example "grid cells use the gridcell role named by their coordinate" and "sign in through the home-page Sign in link".
   - A unit only saw its own ancestors, and test generation cut every ancestor description to 600 characters.
   - So tests were written with invented locators, such as `getByRole('gridcell', {name: 'Region'})` when the rule names cells by coordinate.
3. **Low-effort code generation.** The smaller task reached 55% at 927's high effort and 33% at V8's low effort.
4. **The larger task ran out of context.**
   - `backend/src/app.js` grew to 121 KB, which filled V8's 120–150k-character context on its own.
   - Page files the units needed were hidden. 11 units had edits rejected ("EDIT OLD text not found" or "file not shown"), and those changes were silently dropped.
   - REQ-5-4 got no usable answer after 3 attempts at the same hidden file. REQ-6-1 was rolled back.
5. **Seeded records were unreachable.**
   - After sign-in, the landing page of the larger task listed none of the seeded records, and data models diverged across modules.
   - 21 local tests failed at `Not found: control <seeded record name>`. There were 4 placeholder pages and two URL schemes for the same pages.
6. **Useful repairs were thrown away.**
   - Global repairs of +3/−1, +3/−2 and +3/−3 were all reverted, because V8 rejected any repair that broke even one test.
   - After 5 rejections in a row, repairs stopped with about 1.3 h of the time budget left.
   - The prompt cache hit rate was 18–25%, because the per-unit implementation surface sat before the source files.

## Changes in 929.V1

| Area | Change |
|---|---|
| Test helper | `control()` also matches gridcell, cell, columnheader, rowheader, row and listitem by name. New `h.cell(page, 'A1')`. |
| Shared contracts | Root and module descriptions go in full into every prompt (architecture, units, tests, repairs) as the `GLOBAL CONTRACTS` section. They are cached as a stable prefix, and ancestors are no longer truncated. |
| Quoted values | A mechanical index of every back-quoted value and the requirements that use it (for example, an account used by 24 requirements). The architecture step must produce one consistent seed plan and one navigation map. |
| Contract rules | Home page and signed-in landing page link the main records by exact name. One data model. Files under about 30 KB, with `app.js` only wiring modules. No placeholder pages. |
| Reasoning (`PROFILE='quality'`) | High for all code generation and test repairs. Low for test writing, build fixes and continuations. |
| Context | 240–260k characters (the model accepts 1M tokens). Files are ordered purely by last change, oldest first, for the prefix cache. Unit-specific parts come after the source. |
| Rejected blocks | If nothing applies, the retry shows the targeted files in full. If part applies, a follow-up call re-requests only the rejected blocks, with the files shown. |
| Broken streams | A corrupt SSE chunk is retried without blaming the model. A partial answer of 2000 characters or more is salvaged like a truncated one. |
| Gap notes | Each prompt lists files over the size rule, routes that render stubs, and pages reachable under two URL schemes. |
| Repair focus | The URL where a test failed is mapped through the routes to the page file, which is always put in the repair context. |
| Repair acceptance | A repair is kept when it fixes at least one test and breaks none, or fixes at least 2 more than it breaks with at most 1 broken per 3 fixed. The suite checkpoints every 3 kept repairs and restores the best snapshot. A cause that resists 2 repairs yields to other causes. Stop after 8 consecutive rejections. Up to 2 repair rounds per unit. |
| Time budget | `min(16 h, 720 s × leaves + 1 h)`: about 10.4 h for the larger task (47 leaves) and 5.8 h for the smaller (24 leaves). The 48 h limit is not at risk. |

**Expected cost:** roughly 45–55 CNY for both tasks, against 34.8 CNY for V8.
- By the formula above, this pays off from a pass rate of about 27%.
- 927.V1, with high effort and none of these fixes, reached 32.5%.
- To trade quality for cost, set `PROFILE = 'balanced'` in `budget_policy.py` (V8 behaviour).

No task names, pages or answers are in the package. Everything is derived from the supplied requirement tree at runtime.

## Local verification
- The offline simulation passes every scenario: one-unit and 13-unit tasks; faulty-model variants (express, render crash, truncation, `<think>`, host overwrite, broken specs, test disputes, regressions); gateway variants (controls rejected or ignored, no streaming, no usage, GOAWAY).
- `python -m unittest discover -s tests` runs 44 tests.
