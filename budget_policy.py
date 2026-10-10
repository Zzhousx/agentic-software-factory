"""Cost policy: token ceilings and per-stage reasoning effort (929.V1).

What the three official runs say about the score (overall pass rate p, total cost c in CNY):

    run      p       c      score   score/p
    926.V1   23.5%   24.0   22.38   0.952
    V8       25.5%   34.8   22.91   0.898
    927.V1   32.5%   51.5   28.35   0.872

All three fit  score = p * (80 * p / c) ** 0.2  (two constants, three points; the third point
matches to 0.01). The cost exponent is only ~0.2: +10% pass rate is worth ~+60% cost. 928/V8
cut reasoning to 'low' for code generation and saved 17 CNY, but the pass rate fell (the
smaller task went from 55% to 33%), which cost far more score than the savings earned.

929.V1 therefore spends reasoning where code quality is decided (every code-writing and repair
stage), keeps it low for mechanical stages (test writing, build fixes, continuations of an
answer that was already reasoned), and gets its savings from the prompt cache instead.

PROFILE: 'quality' (default, 929.V1), 'balanced' (928/V8 behaviour), 'lean' (cheapest).
"""
PROFILE = 'quality'

REASONING_PROFILES = {
    'quality': {'architecture': 'high', 'source_generation_first': 'high', 'source_generation': 'high',
                'source_generation_complex': 'high', 'source_continuation': 'low', 'source_continuation_complex': 'low',
                'test_generation': 'low', 'build_repair': 'low', 'test_repair_unit': 'high', 'test_repair': 'high',
                'source_generation_retry': 'low', 'requirement_audit': 'high', 'unit_review': 'high'},
    'balanced': {'architecture': 'high', 'source_generation_first': 'high', 'source_generation': 'low',
                 'source_generation_complex': 'high', 'source_continuation': 'low', 'source_continuation_complex': 'high',
                 'test_generation': 'off', 'build_repair': 'low', 'test_repair_unit': 'low', 'test_repair': 'high',
                 'source_generation_retry': 'low', 'requirement_audit': 'low', 'unit_review': 'low'},
    'lean': {'architecture': 'low', 'source_generation_first': 'low', 'source_generation': 'low',
             'source_generation_complex': 'low', 'source_continuation': 'low', 'source_continuation_complex': 'low',
             'test_generation': 'off', 'build_repair': 'low', 'test_repair_unit': 'low', 'test_repair': 'low',
             'source_generation_retry': 'low', 'requirement_audit': 'low', 'unit_review': 'low'},
}

# Streamed reasoning (chars, ~3.5 chars/token) after which an attempt without an answer is aborted
# and retried at a lower effort. The 927.V1 run had empty answers after 8k-58k reasoning tokens.
REASONING_CAPS = {'test_generation': 60000, 'build_repair': 120000, 'source_generation': 160000,
                  'source_generation_first': 180000, 'source_generation_complex': 160000, 'source_continuation': 120000,
                  'source_continuation_complex': 120000, 'test_repair_unit': 140000, 'test_repair': 150000,
                  'architecture': 150000, 'source_generation_retry': 120000, 'requirement_audit': 150000,
                  'unit_review': 150000}
# 930.V1: caps lowered (~45k reasoning tokens for code). The gateway dropped ~30% of the streams that ran 5+ minutes;
# an answer still reasoning at the cap is aborted and retried at lower effort rather than risking the drop.

# Ceilings per task run (not targets), in CNY at the model's list prices. 932.V1 counts cost instead of raw tokens:
# whole-source prompts are long but mostly served from the prefix cache at 1/30 of the price, so a token ceiling
# would stop a cheap run early. For reference, 930.V1 spent about 27 CNY (larger task) and 12 CNY (smaller task).
PRICES_CNY_PER_M = {'input': 3.0, 'cached': 0.1, 'output': 9.0}
COST_LIMIT_CNY = 70.0
BUCKET_COST_LIMITS = {'planning': 2.0, 'generation': 32.0, 'verification': 8.0, 'repair': 36.0}
# Raw token ceilings kept as a last-resort guard against a gateway that reports no cache hits at all.
GLOBAL_TOKEN_LIMIT = 40_000_000
BUCKET_LIMITS = {'planning': 400_000, 'generation': 18_000_000, 'verification': 3_000_000, 'repair': 20_000_000}


def usage_cost(usage) -> float:
    if not usage:
        return 0.0
    prompt = int(usage.get('prompt_tokens') or 0)
    cached = int(((usage.get('prompt_tokens_details') or {}).get('cached_tokens')) or 0)
    completion = int(usage.get('completion_tokens') or 0)
    return ((prompt - cached) * PRICES_CNY_PER_M['input'] + cached * PRICES_CNY_PER_M['cached']
            + completion * PRICES_CNY_PER_M['output']) / 1e6

# Stop the acceptance repair phase after this many consecutive repairs that were not kept.
MAX_CONSECUTIVE_REJECTED_REPAIRS = 10


def reasoning_for(stage):
    return REASONING_PROFILES.get(PROFILE, {}).get(stage)


def budget_bucket(stage):
    if stage == 'architecture':
        return 'planning'
    if stage in ('source_generation', 'source_generation_first', 'source_generation_complex', 'source_continuation',
                 'source_continuation_complex'):
        return 'generation'
    if stage in ('build_repair', 'test_repair', 'test_repair_unit', 'requirement_audit', 'unit_review'):
        return 'repair'
    return 'verification'
