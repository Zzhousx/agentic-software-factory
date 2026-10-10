"""Entry flow (935.V1): load requirements, set up cost-aware model calls, generate the app.

This entry flow uses requirement-driven generation only. It does not select or ship prebuilt
application implementations; the supplied requirements are interpreted by the model at runtime.
"""
import json
import threading
import time
from pathlib import Path

from budget_policy import (BUCKET_COST_LIMITS, BUCKET_LIMITS, COST_LIMIT_CNY, GLOBAL_TOKEN_LIMIT, REASONING_CAPS,
                           budget_bucket, reasoning_for, usage_cost)
from core_utils import load_requirements, make_client, requirement_leaves
from model_transport import request_json

ROOT = Path(__file__).resolve().parent


def time_budget(leaf_count):
    # Runtime is not scored (48 h limit per task); tokens only weakly (see budget_policy). High-effort code
    # generation takes longer per unit than V8's low effort, and repairs need test time, so the budget grows.
    # 930.V1: +2 min per leaf for retries after dropped connections and the late retry of failed units.
    # 931.V1: +2 min per leaf for the requirement audit of every unit.
    return min(16 * 3600, max(3 * 3600, leaf_count * 960 + 3600))


def run_requirement_evolution(runtime, requirements_dir, output_dir, template_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    # An inherited application (evolution task) is evolved in place, never overwritten or refused.
    inherited = any((output_dir / f).exists() and (output_dir / f).read_bytes() != (template_dir / f).read_bytes()
                    for f in ('frontend/src/App.tsx', 'backend/src/index.js'))
    raw = (requirements_dir / 'requirements.yaml').read_text(encoding='utf-8')
    document, repairs = load_requirements(raw)
    if isinstance(document, list):
        document = {'id': 'ROOT', 'name': 'Application', 'children': document}
    if not isinstance(document, dict):
        raise ValueError('requirements.yaml is not a requirement tree')
    # 935.V1: evolution tasks. Earlier stages' specs, requirement history and plan travel with the project (evogit/).
    import stage_memory
    memory = stage_memory.load(output_dir, document, inherited)
    stage_memory.record_stage(output_dir, document)
    print('[evogit] project: ' + ('inherited application from an earlier stage' if inherited else 'new application from the template') +
          (f"; earlier-stage specs {len(memory['legacy_specs'])} files, requirement history {memory['history_stages']} stage(s), "
           f"earlier plan {'yes' if memory['previous_plan'] else 'no'}" if inherited else '') +
          ('; prerequisites from earlier stages: ' + ', '.join(r['id'] for r in memory['prerequisites']) if memory['prerequisites'] else ''),
          flush=True)
    client, model = make_client()
    leaves = requirement_leaves(document)
    budget = time_budget(len(leaves))
    deadline = time.monotonic() + budget
    status = {'version': '935.V1', 'status': 'running', 'events': [], 'usage': [], 'budget_seconds': budget,
              'requirement_leaves': len(leaves), 'yaml_repairs': repairs, 'inherited_application': inherited,
              'reasoning_policy': __import__('budget_policy').PROFILE,
              'stage_memory': {'legacy_spec_files': len(memory['legacy_specs']), 'history_stages': memory['history_stages'],
                               'previous_plan': bool(memory['previous_plan']),
                               'prerequisites': [r['id'] for r in memory['prerequisites']]}}
    lock = threading.RLock()

    def persist():
        # Spec generation runs in a background thread: serialize bookkeeping, tolerate concurrent mutation.
        with lock:
            for _ in range(5):
                try:
                    usage = [u for u in list(status['usage']) if u]
                    status['known_tokens'] = sum(int(u.get('total_tokens') or 0) for u in usage)
                    status['reasoning_tokens'] = sum(int(((u.get('completion_tokens_details') or {}).get('reasoning_tokens')) or 0) for u in usage)
                    status['cached_prompt_tokens'] = sum(int(((u.get('prompt_tokens_details') or {}).get('cached_tokens')) or 0) for u in usage)
                    status['estimated_cost_cny'] = round(sum(usage_cost(u) for u in usage), 3)
                    text = json.dumps(status, ensure_ascii=False, indent=2)
                    break
                except (RuntimeError, ValueError):
                    time.sleep(0.05)
            else:
                return
            try:
                (output_dir / 'MODEL_STATUS.json').write_text(text, encoding='utf-8')
            except OSError:
                pass

    def call(stage, prompt, validator, seconds=650, **options):
        bucket = budget_bucket(stage)
        with lock:
            persist()
            spent = status.setdefault('stage_tokens', {}).get(bucket, 0)
            spent_cost = status.setdefault('stage_cost_cny', {}).get(bucket, 0.0)
            if (status['known_tokens'] >= GLOBAL_TOKEN_LIMIT or spent >= BUCKET_LIMITS.get(bucket, 10**9)
                    or status.get('estimated_cost_cny', 0) >= COST_LIMIT_CNY or spent_cost >= BUCKET_COST_LIMITS.get(bucket, 10**9)
                    or time.monotonic() >= deadline or seconds < 5):
                status['events'].append({'operation': stage, 'state': 'skipped_budget', 'accepted': False})
                persist()
                return None
        usages = []
        max_tokens = options.pop('max_tokens', 8000)
        policy_stage = options.pop('policy_stage', stage)
        options.setdefault('reasoning', reasoning_for(policy_stage))
        options.setdefault('reasoning_cap', REASONING_CAPS.get(policy_stage))
        api_stage = 'source_generation' if stage.startswith('source_') else stage
        result = request_json(client, model, api_stage, prompt, validator, status['events'], usages, persist,
                              min(deadline, time.monotonic() + seconds), max_tokens=max_tokens, **options)
        with lock:
            status['usage'].extend(usages)
            status['stage_tokens'][bucket] = status['stage_tokens'].get(bucket, 0) + sum(int(u.get('total_tokens') or 0) for u in usages if u)
            status['stage_cost_cny'][bucket] = round(status['stage_cost_cny'].get(bucket, 0.0) + sum(usage_cost(u) for u in usages), 4)
            persist()
        return result

    runtime.events.mark_run_started('935.V1 requirement-driven generation with acceptance-test repair')
    from new_task_driver import run_new_task
    return run_new_task(runtime, output_dir, template_dir, document, call, status, persist, deadline, inherited=inherited,
                        memory=memory)
