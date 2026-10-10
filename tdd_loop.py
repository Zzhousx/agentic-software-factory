"""Acceptance-test-driven repair loop (927.V1; repair acceptance and focus revised in 929.V1).

Per unit: run the unit's requirement-derived specs (plus regression specs of units sharing
files), repair failures once. After all units: full suite, then repair the largest groups
of failures that share a cause (e.g. one missing sign-in control failing 30 tests) until the
time reserve. A repair is kept only if it makes target tests pass without breaking any test
that passed before; periodic full runs guard against drift and restore the best snapshot.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from acceptance import (SpecGenerator, SuiteRunner, describe_leaf, digest, error_excerpt, failed_tests, group_failures,
                        leaves, passed_keys)
from generated_application import (MODE, SHARED, build, changed_paths, contract_check, paths_in, protected_paths,
                                   repair_until_verified, restore_sources, source_context, source_section,
                                   shared_head, source_snapshot, task_header, truncation_hint, unit_view, verify)
from source_blocks import block_parser, write_staged
from acceptance import signature
from budget_policy import MAX_CONSECUTIVE_REJECTED_REPAIRS
from route_map import files_for_urls

UNIT_REPAIR_ROUNDS = 1          # per-unit repair after the review, only for tests that still fail
CHECKPOINT_EVERY = 3            # full suite run after this many kept global repairs
MAX_ATTEMPTS_PER_TEST = 3


def keep_repair(gained: int, lost: int) -> bool:
    """V8 reverted every repair that broke any test, including +3/-1 results, and then stopped after
    five such rejections. A clear net gain is kept; the lost tests become repair targets themselves,
    and checkpoint full runs restore the best snapshot if the suite as a whole gets worse."""
    if gained <= 0:
        return False
    if lost == 0:
        return True
    return gained - lost >= 2 and lost * 2 <= gained


def log(message):
    """Progress for the platform log (the only diagnostics visible without downloading the workspace)."""
    print('[evogit-acceptance] ' + message, flush=True)


def top_failures(result, limit=6):
    groups = {}
    for key, test in failed_tests(result).items():
        groups.setdefault(signature(test), []).append(test)
    rows = sorted(groups.items(), key=lambda item: -len(item[1]))[:limit]
    return '; '.join(f"{len(tests)}x {sig[:100]} (e.g. {tests[0].get('title', '')[:50]}: {error_excerpt(tests[0], 150)})"
                     for sig, tests in rows)


def failure_rows(result, limit=80):
    """Compact failure list kept in MODEL_STATUS.json (downloadable with the workspace) for diagnosis."""
    rows = []
    for key, test in sorted(failed_tests(result).items())[:limit]:
        rows.append({'test': test.get('title', key)[:120], 'file': test.get('file'), 'error': error_excerpt(test, 300),
                     'url': ((test.get('capture') or {}).get('url') or '')[:160]})
    return rows

REPAIR_INTRO = """The acceptance tests below were written from the requirements by an independent QA engineer and FAIL
against the current application. Make the APPLICATION satisfy the requirements so these tests pass.
- Tests are read-only and are not shown to users; fix the product, never special-case tests.
- Read each ERROR and ACCESSIBILITY SNAPSHOT: it shows exactly what the test could see (roles and accessible names).
  Typical causes: missing control/route, wrong or duplicated accessible name, input without label, feature reachable only
  by a different path than the requirements describe, missing seed data, server error, message text not exact.
- If the requirements truly contradict a test (the test asks for something the requirements do not say), leave that part
  alone. If nothing in the application should change, output exactly <<<NO CHANGES>>>.
- Do not break other features: keep names unique on each page, keep existing routes and seed data."""


REVIEW_INTRO = """You implemented the unit below in the previous step. Independent QA tests (not shown to you) check EVERY
statement of its requirements, with one test per scenario, and they also cover what our own tests miss. Review the
CURRENT SOURCE against the requirements statement by statement, fix every gap, and make the failing tests listed below
pass when they follow the requirements:
- each named control, field, link, tab, heading, menu item and message exists where the requirement puts it, with the
  exact accessible name and role (also the GLOBAL CONTRACTS rules for this kind of element);
- each state attribute, selection state, count, ordering, formatting and calculation rule;
- each validation/error rule with its exact message text, and that a rejected action changes nothing;
- the permission and visibility rules for every role and visitor the requirement names;
- persistence: after reload or reopening, the same data and state are shown;
- reachability through the entry points the requirement names, starting from the home page.
Output change blocks for the gaps only. Keep everything that works and every name other requirements rely on.
If the source already satisfies every statement, output exactly <<<NO CHANGES>>>."""


class AcceptanceLoop:
    def __init__(self, root: Path, document, call, status, persist, deadline, on_accept=None):
        self.root, self.document, self.call = Path(root), document, call
        self.status, self.persist, self.deadline = status, persist, deadline
        self.on_accept = on_accept
        self.runner: SuiteRunner | None = None
        self.generator: SpecGenerator | None = None
        self.latest: dict[str, str] = {}          # test key -> latest known status
        self.details: dict[str, dict] = {}        # test key -> latest failure details
        self.unit_of_leaf: dict[str, str] = {}
        self.unit_files: dict[str, set] = {}
        self.unit_dependencies: dict[str, set] = {}
        self.attempts: dict[str, int] = {}
        self.disputed: set[str] = set()
        self.rejections: dict[str, str] = {}      # test key -> what the last reverted attempt broke
        self.leaf_nodes = {str(n.get('id')): n for n in leaves(document)}
        self.report = status['acceptance'] = {'status': 'not_started', 'repairs': [], 'full_runs': []}
        self.legacy_all: dict[str, str] = {}      # earlier-stage specs found in the inherited project (stage_memory)
        self.legacy_specs: dict[str, str] = {}    # ... of which at least one test passes on the inherited app

    def set_legacy(self, specs, leaf_nodes=None):
        """935.V1: specs of earlier stages of an evolving project. Run once on the inherited application in start();
        the files with passing tests then guard every change like this stage's passing tests."""
        self.legacy_all = dict(specs or {})
        for leaf_id, node in (leaf_nodes or {}).items():
            self.leaf_nodes.setdefault(str(leaf_id), node)

    def _legacy_baseline(self):
        if not self.legacy_all or not self.runner or not self.runner.available:
            return
        result = self.runner.run(self.root, self.legacy_all, timeout=1800)
        if result.get('status') != 'executed':
            self.report['legacy'] = {'status': result.get('status'), 'files': len(self.legacy_all)}
            log(f"earlier-stage specs: baseline run {result.get('status')}; not used as guards")
            return
        tests = result.get('tests') or {}
        passing_files = {t['file'] for t in tests.values() if t['status'] == 'passed'}
        self.legacy_specs = {n: c for n, c in self.legacy_all.items() if n in passing_files}
        guarded = {k: t for k, t in tests.items() if t['file'] in self.legacy_specs}
        self._record({'tests': guarded})
        for key, test in guarded.items():
            if test['status'] != 'passed':
                self.disputed.add(key)      # failed already before this stage: not a repair target here
        passed = sum(t['status'] == 'passed' for t in tests.values())
        self.report['legacy'] = {'files': len(self.legacy_all), 'guard_files': len(self.legacy_specs),
                                 'tests': len(tests), 'passed': passed, 'seconds': result.get('seconds')}
        log(f"earlier-stage specs: {passed}/{len(tests)} tests pass on the inherited application; "
            f"{len(self.legacy_specs)} of {len(self.legacy_all)} files kept as regression guards")

    def _legacy_guard(self):
        """Earlier-stage guard specs for per-change reruns (full runs always include them)."""
        return dict(self.legacy_specs) if self.legacy_specs and self._remaining() > 5400 else {}

    def _all_specs(self):
        return {**self.legacy_specs, **self.generator.all_specs()} if self.generator else dict(self.legacy_specs)

    # ------------------------------------------------------------------ setup
    def start(self, units):
        try:
            self.runner = SuiteRunner(self.deadline)
        except Exception as error:
            self.report.update(status='unavailable', reason=f'{type(error).__name__}: {error}'[:300])
            return
        self.report['runner'] = {k: self.runner.tool.get(k) for k in ('ok', 'executable', 'reason')}
        if not self.runner.available:
            self.report.update(status='unavailable', reason=self.runner.tool.get('reason'))
            self.persist()
            return
        req_to_units = {}
        unit_nodes = {}
        def walk_nodes(node):
            if not isinstance(node, dict):
                return []
            out = [node]
            for child in node.get('children', []) or []:
                out.extend(walk_nodes(child))
            return out
        for unit in units:
            self.unit_files.setdefault(unit['id'], set())
            nodes = walk_nodes(unit['requirement'])
            unit_nodes[unit['id']] = nodes
            for node in nodes + [a for a in unit.get('ancestors') or [] if isinstance(a, dict)]:
                if node.get('id') is not None:
                    req_to_units.setdefault(str(node.get('id')), set()).add(unit['id'])
            for leaf in leaves(unit['requirement']):
                self.unit_of_leaf[str(leaf.get('id'))] = unit['id']
        for unit in units:
            deps = set()
            for node in unit_nodes.get(unit['id'], []) + [a for a in unit.get('ancestors') or [] if isinstance(a, dict)]:
                for dep in node.get('dependencies') or []:
                    deps.update(req_to_units.get(str(dep), set()))
            deps.discard(unit['id'])
            self.unit_dependencies[unit['id']] = deps
        info = {k: v for k, v in self.document.items() if k in ('name', 'description')} if isinstance(self.document, dict) else {}
        from stage_memory import spec_store
        self.generator = SpecGenerator(units, self.document, self.call, spec_store(self.root), self.runner,
                                       json.dumps(info, ensure_ascii=False), self.persist, self.deadline - 900)
        self.generator.seed_notes = getattr(self, 'seed_notes', '') or ''
        if self.generator.seed_notes:
            self.report['seed_conflicts'] = self.generator.seed_notes[:2000]
        self.report['status'] = 'running'
        self.generator.start()
        try:
            self._legacy_baseline()
        except Exception as error:   # the guard is optional: never let it stop the run
            self.report['legacy'] = {'status': 'error', 'error': f'{type(error).__name__}: {error}'[:300]}

    @property
    def active(self) -> bool:
        return self.runner is not None and self.runner.available and self.generator is not None

    # ------------------------------------------------------------------ helpers
    def _remaining(self):
        return self.deadline - time.monotonic()

    def _record(self, result):
        for key, test in (result.get('tests') or {}).items():
            self.latest[key] = test['status']
            self.details[key] = test

    def _specs_of_units(self, unit_ids):
        specs = {}
        for unit_id in unit_ids:
            specs.update(self.generator.specs.get(unit_id, {}))
        return specs

    def _units_touching(self, files):
        files = set(files)
        return [u for u, touched in self.unit_files.items() if touched & files]

    def _passing_specs_for(self, unit_ids, limit_files=14):
        specs = {}
        for unit_id in unit_ids:
            for name, content in self.generator.specs.get(unit_id, {}).items():
                if any(self.latest.get(k) == 'passed' for k in self.latest if k.startswith(name + '::')):
                    specs[name] = content
                if len(specs) >= limit_files:
                    return specs
        return specs

    # ------------------------------------------------------------------ per unit
    def prompt_specs(self, unit_id, max_wait=180, limit=26000):
        """The unit's acceptance specs for the implementation prompt (test-first), if ready in time."""
        if not self.active:
            return ''
        self.generator.ready[unit_id].wait(timeout=max(0, min(max_wait, self._remaining() - 3600)))
        specs = self.generator.specs.get(unit_id, {})
        text, size = [], 0
        for name, content in sorted(specs.items()):
            if size + len(content) > limit:
                break
            text.append(f'=== {name} ===\n{content}')
            size += len(content)
        return '\n\n'.join(text)

    def after_unit(self, unit, changed, deadline, allow_repair=True):
        if not self.active:
            return {'status': 'unavailable'}
        self.unit_files.setdefault(unit['id'], set()).update(changed)
        wait = max(0, min(240, self._remaining() - 1800))
        self.generator.ready[unit['id']].wait(timeout=wait)
        own = self.generator.specs.get(unit['id'], {})
        if not own:
            return {'status': 'no_specs', 'generator': self.generator.report.get(unit['id'])}
        regression_units = set(u for u in self._units_touching(changed) if u != unit['id']) | self.unit_dependencies.get(unit['id'], set())
        regression = {**self._legacy_guard(), **self._passing_specs_for(sorted(regression_units), limit_files=20)}
        result = self.runner.run(self.root, {**regression, **own})
        if result.get('status') != 'executed':
            return {'status': result.get('status'), 'reason': str(result.get('reason'))[:300]}
        previously_passing = {k for k, s in self.latest.items() if s == 'passed'}
        defects = {t['file']: t.get('error', '') for k, t in result['tests'].items() if t['file'] in own and t['status'] == 'test_error'}
        if defects and self._remaining() > 2400:
            rewritten = self.generator.rewrite(unit, defects)
            if rewritten:
                own = self.generator.specs.get(unit['id'], {})
                again = self.runner.run(self.root, rewritten)
                if again.get('status') == 'executed':
                    result['tests'] = {k: t for k, t in result['tests'].items() if t['file'] not in rewritten}
                    result['tests'].update(again['tests'])
                log(f"unit {unit['id']}: rewrote {len(rewritten)} spec file(s) whose own code threw")
        self._record(result)
        unit_keys = [k for k, t in result['tests'].items() if t['file'] in own]
        broken = [k for k in failed_tests(result) if k in previously_passing]
        failing = [k for k in failed_tests(result) if k in unit_keys or k in broken]
        summary = {'tests': len(unit_keys), 'passed': sum(result['tests'][k]['status'] == 'passed' for k in unit_keys),
                   'regressions': len(broken), 'repairs': [], 'repair_allowed': bool(allow_repair)}
        if allow_repair and self._remaining() > 1800:
            review = self.review_unit(unit, failing, result)
            summary['review'] = {k: review.get(k) for k in ('accepted', 'gained', 'lost', 'reason')}
            if review.get('accepted'):
                result = {'tests': {k: self.details[k] for k in self.latest}, 'server_errors': result.get('server_errors', [])}
                failing = [k for k in unit_keys + broken if self.latest.get(k) not in ('passed', None)
                           and self.details.get(k, {}).get('status') not in ('load_error', 'skipped', 'test_error')]
                broken = [k for k in broken if self.latest.get(k) != 'passed']
                summary['passed_after_repair'] = sum(self.latest.get(k) == 'passed' for k in unit_keys)
        rounds = 0
        while failing and allow_repair and self._remaining() > 1800 and rounds < UNIT_REPAIR_ROUNDS:
            rounds += 1
            ordered = broken + [k for group in group_failures(result) for k in group if k in failing and k not in broken]
            outcome = self.repair(ordered[:6], result, 'unit ' + unit['id'])
            summary['repairs'].append(outcome)
            if not outcome.get('accepted'):
                break
            summary['passed_after_repair'] = sum(self.latest.get(k) == 'passed' for k in unit_keys)
            # Second round only on what is still failing (latest statuses from the kept repair's rerun).
            result = {'tests': {k: self.details[k] for k in self.latest}, 'server_errors': result.get('server_errors', [])}
            failing = [k for k in unit_keys + broken if self.latest.get(k) not in ('passed', None)
                       and self.details.get(k, {}).get('status') not in ('load_error', 'skipped', 'test_error')]
            broken = [k for k in broken if self.latest.get(k) != 'passed']
        log(f"unit {unit['id']}: own tests {summary['passed']}/{summary['tests']}, regressions {summary['regressions']}"
            + (f", after repair {summary.get('passed_after_repair')}" if 'passed_after_repair' in summary else '')
            + ("" if summary['passed'] == summary['tests'] else ' | failing: ' + top_failures({'tests': {k: result['tests'][k] for k in unit_keys}}, 3)))
        self.persist()
        return summary

    # ------------------------------------------------------------------ repair
    def repair(self, keys, result, label):
        record = self._repair(keys, result, label)
        if record.get('reason') and not record.get('gained') and 'gained' not in record:
            log(f"repair [{label}] not applied: {record['reason']} ({len(keys)} target tests)")
        return record

    def _repair(self, keys, result, label):
        keys = [k for k in keys if k in result.get('tests', {})]
        if not keys:
            return {'accepted': False, 'reason': 'nothing to repair'}
        specs_all = self._all_specs()
        before = source_snapshot(self.root)
        leaf_ids = list(dict.fromkeys(result['tests'][k]['leaf'] for k in keys))
        requirement_text = '\n\n'.join(describe_leaf(self.leaf_nodes[l], 8000) for l in leaf_ids if l in self.leaf_nodes)[:24000]
        failure_text = digest(result, keys, specs_all)
        units = {self.unit_of_leaf.get(l) for l in leaf_ids}
        urls = [((result['tests'][k].get('capture') or {}).get('url')) for k in keys]
        page_files = files_for_urls(self.root, urls)
        focus = page_files + paths_in(failure_text, before) + sorted(set().union(*[self.unit_files.get(u, set()) for u in units if u]))
        focus = list(dict.fromkeys(focus))
        context = source_context(self.root, focus=focus[:16], unit_text=requirement_text + '\n' + failure_text[:6000])
        for key in keys:
            self.attempts[key] = self.attempts.get(key, 0) + 1
        previous = list(dict.fromkeys(self.rejections[k] for k in keys if k in self.rejections))
        staged = self.call('test_repair', shared_head() + source_section(self.root, context) + task_header('test_repair') +
                           REPAIR_INTRO + '\n\nREQUIREMENTS UNDER TEST:\n' + requirement_text +
                           ('\n\nFILES RENDERING THE PAGES WHERE THE TESTS FAILED: ' + ', '.join(page_files) if page_files else '') +
                           '\n\nFAILING TESTS:\n' + failure_text +
                           ('\n\nEARLIER ATTEMPTS AT THESE FAILURES WERE REVERTED because they broke passing tests. Find a fix '
                            'that keeps those tests passing:\n' + '\n'.join(previous)[:6000] if previous else ''),
                           lambda d: d, 1500, max_tokens=24000, parser=block_parser(self.root, set(context['files']), protected_paths()),
                           attempt_timeout=1200, truncation_hint=truncation_hint(),
                           policy_stage='test_repair_unit' if label.startswith('unit') else 'test_repair')
        record = {'label': label, 'tests': keys, 'accepted': False}
        self.report['repairs'].append(record)
        if staged is None:
            record['reason'] = 'no_valid_response'
            return record
        if not staged['staged']:
            self.disputed.update(keys)
            record['reason'] = 'model_reports_no_change_needed'
            return record
        write_staged(self.root, staged['staged'])
        return self.evaluate_change(before, staged, keys, units, label, record, page_files)

    def evaluate_change(self, before, staged, keys, units, label, record, page_files=(), allow_neutral=False):
        """Build + runtime check, then rerun the target tests and every passing test that could be affected.
        Keep the change by keep_repair(); otherwise restore `before` and remember what broke for the next attempt."""
        check = (contract_check(self.root, staged) if MODE['fixed_host'] else None) or verify(self.root, self.deadline)
        check, _ = repair_until_verified(self.root, self.call, self.deadline, before, check, label + ' (change)', rounds=1)
        if not check['ok']:
            restore_sources(self.root, before)
            build(self.root, self.deadline)
            record['reason'] = 'verification_failed: ' + str(check.get('stage'))
            return record
        changed = changed_paths(before, source_snapshot(self.root))
        specs_all = self._all_specs()
        target_files = {self.details[k]['file'] for k in keys if k in self.details}
        scope_units = set(self._units_touching(changed)) | {u for u in units if u}
        for unit_id in list(scope_units):
            scope_units.update(self.unit_dependencies.get(unit_id, set()))
        pool = self._passing_specs_for(scope_units, limit_files=16)
        subset = {n: specs_all[n] for n in target_files if n in specs_all}
        for unit_id in units:
            if unit_id:
                subset.update(self.generator.specs.get(unit_id, {}))
        subset.update(pool)
        subset.update(self._legacy_guard())
        rerun = self.runner.run(self.root, subset)
        if rerun.get('status') != 'executed':
            restore_sources(self.root, before)
            build(self.root, self.deadline)
            record['reason'] = 'rerun_' + str(rerun.get('status'))
            return record
        passed_now = passed_keys(rerun)
        gained = [k for k in keys if k in passed_now]
        lost = [k for k, t in rerun['tests'].items() if self.latest.get(k) == 'passed' and t['status'] != 'passed']
        record.update(gained=len(gained), lost=len(lost), changed=changed)
        neutral = allow_neutral and not lost      # an audit change that breaks nothing
        if neutral or keep_repair(len(gained), len(lost)):
            record['accepted'] = True
            self._record(rerun)
            for unit_id in units:
                if unit_id:
                    self.unit_files.setdefault(unit_id, set()).update(changed)
            if self.on_accept:
                try:
                    self.on_accept(label)
                except Exception:
                    pass
        else:
            restore_sources(self.root, before)
            build(self.root, self.deadline)
            record['reason'] = 'no_net_gain' if not gained else 'regressions'
            if lost:
                note = (f"- an attempt changing {', '.join(changed[:5])} fixed {len(gained)} but broke: " +
                        "; ".join(f"{rerun['tests'][k].get('title', k)[:70]} ({error_excerpt(rerun['tests'][k], 140)})" for k in lost[:4]))
                for key in keys:
                    self.rejections[key] = note
        log(f"{'change' if not keys else 'repair'} [{label}] {'KEPT' if record['accepted'] else 'reverted'}: +{len(gained)} fixed, "
            f"-{len(lost)} broken, files {', '.join(changed[:6])}" + (f" (page files {', '.join(page_files[:3])})" if page_files else ''))
        self.persist()
        return record

    # ------------------------------------------------------------------ review
    def review_unit(self, unit, failing, result):
        """One call per unit right after its tests (932.V1): audit the code against every statement and scenario
        of the unit's requirements AND fix the unit's failing tests. Replaces 930's up-to-two repair rounds and
        931's separate audit phase: the code is still fresh, and one call does both jobs on the shared cached prefix.
        Kept when no passing test breaks (or by the repair rule when it also fixes failing tests)."""
        unit_id = unit['id']
        own = self.generator.specs.get(unit_id, {})
        own_keys = [k for k in self.latest if k.split('::', 1)[0] in own]
        passed = sum(1 for k in own_keys if self.latest.get(k) == 'passed')
        before = source_snapshot(self.root)
        leaf_ids = [str(l.get('id')) for l in leaves(unit['requirement']) if l.get('id')]
        requirement_text = '\n\n'.join(describe_leaf(self.leaf_nodes[l], 8000) for l in leaf_ids if l in self.leaf_nodes)[:30000]
        failure_text = digest(result, failing[:6], self._all_specs(), limit=18000) if failing else ''
        urls = [((result['tests'][k].get('capture') or {}).get('url')) for k in failing if k in result.get('tests', {})]
        page_files = files_for_urls(self.root, urls)
        focus = page_files + sorted(self.unit_files.get(unit_id, set()))
        context = source_context(self.root, focus=focus[:16], unit_text=requirement_text + '\n' + failure_text[:4000])
        specs_text, size = [], 0
        for name, content in sorted(own.items()):
            if size + len(content) > 16000:
                break
            specs_text.append(f'=== {name} ===\n{content}')
            size += len(content)
        staged = self.call('unit_review', shared_head() + source_section(self.root, context) +
                           task_header('unit_review', f'(review of unit {unit_id})') + REVIEW_INTRO +
                           f'\n\nOUR OWN ACCEPTANCE TESTS FOR THIS UNIT: {passed} of {len(own_keys)} pass.' +
                           '\n\nREQUIREMENTS OF THIS UNIT (every statement and every scenario):\n' + requirement_text +
                           ('\n\nFAILING TESTS OF THIS UNIT (fix the application, not the tests):\n' + failure_text if failure_text else '') +
                           ('\n\nITS ACCEPTANCE TESTS (for reference; they do not cover everything):\n' + '\n\n'.join(specs_text)
                            if specs_text else ''),
                           lambda d: d, 1500, max_tokens=24000, parser=block_parser(self.root, set(context['files']), protected_paths()),
                           attempt_timeout=1200, truncation_hint=truncation_hint(), policy_stage='unit_review')
        record = {'label': 'review ' + unit_id, 'unit': unit_id, 'tests': failing, 'accepted': False}
        self.report.setdefault('reviews', []).append(record)
        if staged is None:
            record['reason'] = 'no_valid_response'
            log(f'review [{unit_id}]: no usable answer')
            return record
        if not staged['staged']:
            record['reason'] = 'no_gaps_found'
            log(f'review [{unit_id}]: no gaps found' + (f' ({len(failing)} failing tests left to the targeted repair)' if failing else ''))
            return record
        write_staged(self.root, staged['staged'])
        return self.evaluate_change(before, staged, failing, {unit_id}, 'review ' + unit_id, record, page_files, allow_neutral=True)

    # ------------------------------------------------------------------ global
    def full_run(self, tag):
        specs = self._all_specs()
        result = self.runner.run(self.root, specs, timeout=3600)
        if result.get('status') == 'executed':
            self._record(result)
        entry = {'tag': tag, 'status': result.get('status'), 'tests': len(result.get('tests', {})),
                 'passed': len(passed_keys(result)), 'seconds': result.get('seconds')}
        self.report['full_runs'].append(entry)
        if result.get('status') == 'executed':
            self.report['failures_' + tag] = failure_rows(result)
        legacy = sum(1 for k in (result.get('tests') or {}) if k.split('::', 1)[0] in self.legacy_specs)
        if legacy:
            entry['legacy_tests'] = legacy
            entry['legacy_passed'] = sum(1 for k, t in result['tests'].items()
                                         if k.split('::', 1)[0] in self.legacy_specs and t['status'] == 'passed')
        log(f"full run [{tag}]: {entry['passed']}/{entry['tests']} own tests pass ({entry['seconds']}s)"
            + (f" incl. earlier-stage guards {entry['legacy_passed']}/{legacy}" if legacy else '')
            + (' | top failure causes: ' + top_failures(result) if result.get('status') == 'executed' and entry['passed'] < entry['tests'] else '')
            + ('' if result.get('status') == 'executed' else ' | ' + str(result.get('status')) + ': ' + str(result.get('reason'))[:200]))
        if result.get('server_errors'):
            log('server errors: ' + ' || '.join(e.splitlines()[0][:160] for e in result['server_errors'][-4:]))
        self.persist()
        return result

    def global_phase(self, reserve=900, units=None):
        if not self.active:
            return
        self.generator.join(timeout=max(0, min(1800, self._remaining() - reserve - 1200)))
        self.generator.stop.set()
        result = self.full_run('after_units')
        if result.get('status') != 'executed':
            return
        best_count, best_snapshot = len(passed_keys(result)), source_snapshot(self.root)
        accepted_since_full = 0
        rejected_in_a_row = 0
        failed_causes: dict[str, int] = {}
        while self._remaining() > reserve + 900:
            if rejected_in_a_row >= MAX_CONSECUTIVE_REJECTED_REPAIRS:
                log(f'stopping repairs after {rejected_in_a_row} consecutive repairs that were not kept (cost control)')
                break
            candidates = []
            for group in group_failures(result):
                cause = signature(result['tests'][group[0]])
                if failed_causes.get(cause, 0) >= 2:
                    continue    # this cause resisted two repairs: give the other causes their turn
                keys = [k for k in group if self.attempts.get(k, 0) < MAX_ATTEMPTS_PER_TEST and k not in self.disputed]
                if keys:
                    candidates.append((cause, keys))
            if not candidates:
                break
            # 935.V1: this stage's failures first; earlier-stage guard tests only after them.
            candidates.sort(key=lambda item: all(k.split('::', 1)[0] in self.legacy_specs for k in item[1]))
            cause, keys = candidates[0]
            outcome = self.repair(keys[:6], result, 'global')
            # Keep the latest failure details for the next grouping.
            result = {'tests': {k: self.details[k] for k in self.latest}, 'server_errors': result.get('server_errors', [])}
            if outcome.get('accepted'):
                accepted_since_full += 1
                rejected_in_a_row = 0
            else:
                rejected_in_a_row += 1
                failed_causes[cause] = failed_causes.get(cause, 0) + 1
            if accepted_since_full >= CHECKPOINT_EVERY and self._remaining() > reserve + 1200:
                accepted_since_full = 0
                full = self.full_run('checkpoint')
                if full.get('status') == 'executed':
                    count = len(passed_keys(full))
                    if count < best_count:
                        restore_sources(self.root, best_snapshot)
                        build(self.root, self.deadline)
                        self.full_run('restored_best')
                    else:
                        best_count, best_snapshot = count, source_snapshot(self.root)
                    result = {'tests': {k: self.details[k] for k in self.latest}, 'server_errors': full.get('server_errors', [])}
        if self._remaining() > reserve + 600:
            final = self.full_run('final')
            if final.get('status') == 'executed' and len(passed_keys(final)) < best_count:
                restore_sources(self.root, best_snapshot)
                build(self.root, self.deadline)
                self.report['restored_best'] = best_count
        self.report['status'] = 'done'
        self.persist()

    def stop(self):
        if self.generator is not None:
            self.generator.stop.set()
