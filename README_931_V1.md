# 931.V1 — built on 930.V1

Upload `931.V1.zip` as a new submission. The layout is unchanged. Everything in 929/930 is kept (see their READMEs).

## What the 930.V1 logs show

| | larger task | smaller task |
|---|---|---|
| units kept | 37 / 37 | 22 / 22 |
| own tests at the end | 50 / 83 | 54 / 58 |
| dropped streams | none this time (one cut answer salvaged) | none |
| tokens (prompt / cached / completion) | 5.68M / 1.87M / 1.76M ≈ 27 CNY | 1.84M / 0.71M / 0.91M ≈ 12 CNY |

The 930 fixes held: every unit was built. The official pass rate stayed low for three reasons.

1. **Our tests saw only part of the scenarios.**
   - Each task has exactly 100 scenarios, one hidden test each.
   - Scenario steps repeat long boilerplate, and every requirement's text was cut at 4,000 characters for the test writer and at 3,000 for repair prompts.
   - As a result, the test writer saw 60 of 100 scenarios on the smaller task and 80 of 100 on the larger. Our own test counts match exactly: 58 and 83.
   - About 40% of the smaller task's hidden checks never had a local test.
2. **Passing our tests does not mean a requirement is complete.**
   - On the smaller task, 93% of our own tests passed while the official rate stayed low.
   - Nothing ever compared the finished code against the full requirement text.
3. **The larger task's repair phase stalled.**
   - 8 repairs in a row were reverted, and repairs stopped with 6.5 of the 12 hours unused.
   - Several attempts were repeated with an identical result (+4/−2 twice, +2/−1 twice, +1/−3 twice), because the second try did not know what the first one broke.

## Changes

| Area | Change |
|---|---|
| Every scenario visible | Requirement text drops sentences already shown for that requirement. All 100/100 scenarios now fit; the largest requirement is 7.3k characters. Used for test writing (12k limit) and repairs (8k per requirement). |
| One test per scenario | The prompt states `TESTS REQUIRED: <id>: N`, and tests are titled `<id> #k`. Scenarios with placeholder wording ("the requested workflow") map to different behaviors of the description: main path, other inputs, each error rule with its exact message, permissions, states, reload. Every statement must be asserted somewhere. An answer with too few tests is sent back once. |
| Requirement audit (new) | After the first full test run, the model reviews each unit's code against its requirement statement by statement. It checks names and roles, states, exact messages, rejected actions changing nothing, permissions, persistence and entry points, then fixes the gaps. A change is kept if it breaks no passing test. The phase uses at most 55% of the remaining time and is followed by a full run; if our pass count drops, all audit changes are reverted. |
| Repair memory | A reverted repair records which tests it broke and why. The next attempt at the same failures gets that list. |
| Repair acceptance | Fixes at least 2 more than it breaks, with at most 1 broken per 2 fixed (+4/−2 is kept). The phase stops after 10 consecutive rejections instead of 8. |
| Seed plan | Tests can only sign in with stated credentials. An account whose password is given gets every role its scenarios need. Accounts named without a password get the same password. |
| App rules | No `window.alert/confirm/prompt` (automated browsers dismiss them); confirmations are in-page dialogs. Text inputs commit on Enter. |
| Time budget | `min(16 h, 960 s × leaves + 1 h)`: about 13.5 h for the larger task and 7.4 h for the smaller. |

**Cost:**
- About 0.25 CNY per audited unit (37 + 22 units), plus about 40 more generated tests. Estimate: +15–18 CNY in total.
- By the fitted score formula, this pays off if the overall pass rate rises by about 7% relative (for example 40% to 43%).
- `PROFILE = 'balanced'` in `budget_policy.py` makes audits and code generation use low effort.

## Local verification
- The offline simulation passes every earlier scenario plus the new audit cases: an audit change that breaks tests is reverted, a harmless one is kept, and "no gaps" leaves the code unchanged.
- `python -m unittest discover -s tests` runs 59 tests.
