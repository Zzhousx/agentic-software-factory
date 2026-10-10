# 932.V1 — a different working pattern: cache-first prompts, review-driven units, product conventions

Upload `932.V1.zip` as a new submission. The layout is unchanged. The robustness work of 929–931 is kept: dropped-stream handling, the late retry of lost units, per-test state reset, all scenarios visible, one test per scenario, and seed-conflict planning.

## What changes, and why

### 1. One cached prefix for every code-writing call (cost)

In the 930.V1 logs, only 30% of prompt tokens came from the provider's cache:

| task | stage | prompt tokens | cached | est. cost |
|---|---|---|---|---|
| larger | repairs | 2.9M | 30% | 11.1 CNY |
| larger | generation | 2.4M | 31% | 13.5 CNY |
| smaller | generation | 0.9M | 42% | 6.4 CNY |

Two causes:
1. Each stage started its prompt with its own name, so generation, build fixes, repairs and reviews never shared a cached prefix.
2. Source files were chosen per call by relevance, so the file list changed from call to call and the cached prefix ended early.

Now every code-writing call uses the same prompt start: the rules, the global contracts, the plan, and then the **whole source** (up to 480k characters), oldest file first. Everything specific to the step goes after it.
- Consecutive calls reuse almost the whole prefix at 1/30 of the input price.
- Every call sees every file, so there are fewer "file not shown" edits and fewer inconsistencies.
- Budgets are now counted in estimated CNY rather than raw tokens, because long cached prompts are cheap. The final log line prints the cost per stage.

### 2. Implement → test → review, per unit (pass rate and cost)

Old per-unit flow: implement, run the tests, then up to two repair rounds that only looked at failing tests. 931 added a separate audit pass over all units at the end.

932 replaces both with a single **review call right after each unit's tests**. The model checks its fresh code against every statement and scenario of the unit's requirements:
- names and roles, and state attributes;
- exact messages, and that a rejected action changes nothing;
- permissions, persistence after reload, and entry points;
- in the same call, it fixes the unit's failing tests.

The review is kept if it breaks no passing test. If it answers "no gaps" while tests still fail, those tests still get the targeted repair. At most one targeted repair follows, and only for tests that still fail. The cross-unit repair phase at the end is unchanged.

### 3. Product conventions (pass rate)

Both tasks imitate a real product. The requirements define most names, but not everything, for example URL patterns, where the account menu sits, or what Enter, Tab, double-click or right-click do. The planning step now also returns `conventions`:
- the product the application imitates;
- its conventions for everything the requirements leave open (URL pattern of each kind of page, titles and headings, menu placement, default labels, keyboard and mouse behaviour).

These go into every prompt through the plan. The contract says the requirement text always wins over a convention.

## Expected cost
- About the same number of large calls as 930.V1: one generation and one review per unit, plus fewer repairs.
- With most input served from cache, the estimate is about 20–26 CNY for the larger task and 9–12 CNY for the smaller.
- For comparison, 930.V1 cost about 27 and 12 CNY without reviews, and 931.V1 would have added about 15 CNY for its separate audit.

## Local verification
- The offline simulation passes every scenario. It includes review behaviour: a review that fixes failing tests is kept; one that breaks tests is reverted; a harmless one is kept; "no gaps found" with failing tests leaves them to the targeted repair.
- `python -m unittest discover -s tests` runs 58 tests. The new tests check that every stage shares the head and source prefix, that the source is complete and ordered by age, and that conventions reach every prompt.
