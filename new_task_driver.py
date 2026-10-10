"""End-to-end unknown-task path (935.V1). Never exits without a runnable, verified-as-possible app."""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from generated_application import (NoGeneratedSource, StarterBuildError, build, generate, source_snapshot,
                                   restore_sources, verify)
from acceptance import cross_cutting_text
from tdd_loop import AcceptanceLoop


def install_fallback_engine(root, template, document):
    """No model output could be verified: deliver the clean, runnable template (no prebuilt task pages)."""
    from core_utils import copy_template
    for part in ('frontend/src', 'backend/src'):
        shutil.rmtree(root / part, ignore_errors=True)
    copy_template(template, root)


def clean_runtime_state(root):
    """Official tests expect fresh seed data: drop anything written by local runs."""
    backend = Path(root) / 'backend'
    if not backend.exists():
        return
    for entry in backend.iterdir():
        if entry.is_dir() and entry.name in {'data', '.data', 'db', 'storage', 'backend', '.arc-test-db'}:
            shutil.rmtree(entry, ignore_errors=True)
        elif entry.is_file() and entry.suffix in {'.db', '.sqlite', '.sqlite3'}:
            entry.unlink()


def _git(root, *args):
    from core_utils import git
    return git(root, *args)


def _commit(root, message):
    from stage_memory import FOLDER
    paths = ['frontend/src', 'backend/src'] + ([FOLDER] if (Path(root) / FOLDER).exists() else [])
    _git(root, 'add', '-A', '--', *paths)
    _git(root, 'commit', '--allow-empty', '-m', message)
    return _git(root, 'rev-parse', 'HEAD')


def run_new_task(runtime, root, template, document, call, status, persist, deadline, inherited=False, memory=None):
    try:
        runtime.git.ensure_repo(create_initial_commit=False)
    except Exception as error:
        status['git_error'] = str(error)[:300]
    budget = status.get('budget_seconds', 3600)
    # Units stop starting once only the test-repair + delivery share of the budget is left.
    reserve = int(min(7200, max(1800, budget * 0.22)))
    final_reserve = 900

    def on_unit(unit_id):
        runtime.events.mark_implementation_done(unit_id, 'verified build + runtime smoke')
        _commit(root, f'935.V1 unit {unit_id} (verified build + runtime)')

    loop = AcceptanceLoop(root, document, call, status, persist, deadline,
                          on_accept=lambda label: _commit(root, f'935.V1 acceptance repair ({label})'))
    if memory:
        loop.set_legacy(memory.get('legacy_specs') or {}, memory.get('legacy_leaves') or {})
    delivery = 'generated'
    units = []
    print('[evogit] reasoning policy: ' + str(status.get('reasoning_policy')) + '; time budget ' + str(budget) + 's', flush=True)
    try:
        units = generate(root, template, document, call, status, persist, deadline, inherited=inherited, on_unit=on_unit,
                         reserve=reserve, loop=loop, shared_text=cross_cutting_text(document), memory=memory)
    except (NoGeneratedSource, StarterBuildError) as error:
        loop.stop()
        status['generation_failure'] = {'type': type(error).__name__, 'detail': str(error)[:1500]}
        delivery = 'template_fallback'
        if not inherited:
            install_fallback_engine(root, template, document)
        build(root, deadline, install=not (root / 'frontend' / 'node_modules').exists())
    status['delivery'] = delivery
    persist()
    best = _commit(root, '935.V1 generated application (' + delivery + ')')
    good_snapshot = source_snapshot(root)

    # ---- Acceptance-test repair phase (requirement-derived Playwright specs) -----------
    if delivery == 'generated':
        try:
            loop.global_phase(reserve=final_reserve, units=[u for u, row in zip(units, status['generation']['units'])
                                                            if row.get('status') in ('verified_build_and_runtime', 'already_satisfied')])
        except Exception as error:  # the loop must never cost the delivery
            status['acceptance_error'] = f'{type(error).__name__}: {error}'[:500]
        loop.stop()
        good_snapshot = source_snapshot(root)
        best = _commit(root, '935.V1 after acceptance repairs')

    # ---- Final delivery gate: what the platform will run must build and serve ----------
    # The bare template renders an empty page by design; only generated apps get the browser render check.
    final = verify(root, deadline, browser=delivery == 'generated')
    if not final['ok']:
        status['final_verification_failure'] = {'stage': final.get('stage'), 'error': final.get('error', '')[:1500]}
        restore_sources(root, good_snapshot)
        final = verify(root, deadline)
        if not final['ok'] and not inherited:
            install_fallback_engine(root, template, document)
            build(root, deadline)
            final = verify(root, deadline, browser=False)
            status['delivery'] = 'template_fallback_after_final_failure'
    status['final_verification'] = {'ok': final['ok'], 'stage': final.get('stage'), 'render': (final.get('smoke') or {}).get('render', {}).get('status')}
    clean_runtime_state(root)
    generation = status.get('generation') or {'units': [], 'units_total': 0}
    acceptance = status.get('acceptance') or {}
    runs = acceptance.get('full_runs') or []
    last = runs[-1] if runs else {}
    status.update(status='delivered' if final['ok'] else 'delivered_unverified', best_commit=best,
                  evaluation_scope='requirement-derived Playwright specs written by the agent; not the external evaluation',
                  acceptance_summary={'tests': last.get('tests'), 'passed': last.get('passed'),
                                      'repairs_attempted': len(acceptance.get('repairs', [])),
                                      'repairs_accepted': sum(bool(r.get('accepted')) for r in acceptance.get('repairs', []))},
                  generated_units=sum(r.get('status') in ('verified_build_and_runtime', 'already_satisfied') for r in generation['units']))
    if status['generated_units'] < generation.get('units_total', 0):
        status['partial_implementation'] = True
    persist()
    usage = [u for u in status.get('usage', []) if u]
    prompt = sum(int(u.get('prompt_tokens') or 0) for u in usage)
    completion = sum(int(u.get('completion_tokens') or 0) for u in usage)
    print(f"[evogit] done: delivery={status['delivery']} units {status['generated_units']}/{generation.get('units_total', 0)} "
          f"own tests {last.get('passed')}/{last.get('tests')} | tokens prompt {prompt} (cached {status.get('cached_prompt_tokens')}) "
          f"completion {completion} (reasoning {status.get('reasoning_tokens')}) | est. cost {status.get('estimated_cost_cny')} CNY "
          f"by stage {status.get('stage_cost_cny')}", flush=True)
    runtime.events.mark_run_completed('935.V1: ' + status['status'] + '; delivery=' + status['delivery'] +
                                      f"; verified units {status['generated_units']}/{generation.get('units_total', 0)}"
                                      f"; own acceptance {last.get('passed')}/{last.get('tests')}")
