"""935.V1: evolution-task memory, earlier-stage regression guards, planning time cap, revert-safe cache order."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generated_application as ga  # noqa: E402
import model_transport  # noqa: E402
import stage_memory  # noqa: E402
from test_928_cost_controls import Client, _Chunk, ok_stream  # noqa: E402


def doc(ids, refs=''):
    return {'id': 'ROOT', 'name': 'Stage', 'description': 'Root. ' + refs,
            'children': [{'id': i, 'name': 'Feature ' + i, 'type': 'ATOMIC', 'description': 'Does ' + i + '. ' + refs,
                          'dependencies': [], 'scenarios': [{'name': i + ' s1'}]} for i in ids]}


class StageMemoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_fresh_project_drops_foreign_memory_and_lists_prerequisites(self):
        (self.root / 'evogit' / 'legacy').mkdir(parents=True)
        (self.root / 'evogit' / 'legacy' / 'REQ-9.spec.ts').write_text('test()')
        document = doc(['REQ-3-1'], 'This feature builds on REQ-1-1-2 (Sign In with an Existing\n Account).')
        document['children'][0]['dependencies'] = ['REQ-4-3']
        info = stage_memory.load(self.root, document, inherited=False)
        self.assertFalse((self.root / 'evogit').exists())
        self.assertEqual([r['id'] for r in info['prerequisites']], ['REQ-1-1-2', 'REQ-4-3'])
        self.assertEqual(info['prerequisites'][0]['name'], 'Sign In with an Existing Account')
        text = stage_memory.prerequisites_text(info['prerequisites'], inherited=False)
        self.assertIn('no earlier application was inherited', text)

    def test_inherited_project_moves_specs_to_legacy_and_reads_history(self):
        first = doc(['REQ-1-1', 'REQ-1-2'])
        stage_memory.record_stage(self.root, first)
        stage_memory.save_plan(self.root, {'architecture': 'Routes /login and /repos', 'conventions': 'Like product X'})
        store = stage_memory.spec_store(self.root)
        store.mkdir(parents=True)
        (store / 'REQ-1-1.spec.ts').write_text("test('REQ-1-1 #1: a', async () => {})")
        (store / 'REQ-2-1.spec.ts').write_text("test('REQ-2-1 #1: re-tested now', async () => {})")
        second = doc(['REQ-2-1'], 'Builds on REQ-1-1 (Feature REQ-1-1).')
        info = stage_memory.load(self.root, second, inherited=True)
        self.assertEqual(sorted(info['legacy_specs']), ['REQ-1-1.spec.ts'], 'specs of ids re-tested by this stage are dropped')
        self.assertTrue((self.root / 'evogit' / 'legacy' / 'REQ-1-1.spec.ts').exists())
        self.assertFalse(store.exists(), 'acceptance/ is emptied for the new stage')
        self.assertIn('REQ-1-1', info['legacy_leaves'])
        self.assertEqual(info['history_stages'], 1)
        self.assertIn('Routes /login', info['previous_plan']['architecture'])
        self.assertIn('REQ-1-2: Feature REQ-1-2', info['previous_outline'])
        self.assertEqual(info['prerequisites'][0]['id'], 'REQ-1-1')
        self.assertIn('Does REQ-1-1', info['prerequisites'][0]['description'], 'earlier description from history')
        stage_memory.record_stage(self.root, second)
        stage_memory.record_stage(self.root, second)
        self.assertEqual(len(list((self.root / 'evogit' / 'history').glob('*.json'))), 2, 'same stage recorded once')
        text = stage_memory.existing_application_text(self.root, info, 'Routes: /login')
        self.assertIn('PLAN OF THE PREVIOUS STAGE', text)
        self.assertIn('Routes: /login', text)

    def test_fallback_plan_is_not_saved(self):
        stage_memory.save_plan(self.root, {'architecture': 'x' * 50, 'fallback': True})
        self.assertFalse((self.root / 'evogit' / 'plan.json').exists())


class _FakeRunner:
    available = True
    tool = {'ok': True}

    def __init__(self, results):
        self.results, self.calls = list(results), []

    def run(self, root, specs, timeout=1800):
        self.calls.append(sorted(specs))
        return self.results.pop(0)


class LegacyGuardTests(unittest.TestCase):
    def test_baseline_keeps_passing_files_and_disputes_old_failures(self):
        import tdd_loop
        status = {}
        loop = tdd_loop.AcceptanceLoop(Path(tempfile.mkdtemp()), doc(['REQ-2-1']), None, status, lambda: None,
                                       time.monotonic() + 20000)
        loop.set_legacy({'REQ-1-1.spec.ts': 'a', 'REQ-1-2.spec.ts': 'b'}, {'REQ-1-1': {'id': 'REQ-1-1', 'name': 'Sign in'}})
        tests = {'REQ-1-1.spec.ts::REQ-1-1 #1: x': {'file': 'REQ-1-1.spec.ts', 'status': 'passed', 'leaf': 'REQ-1-1'},
                 'REQ-1-1.spec.ts::REQ-1-1 #2: y': {'file': 'REQ-1-1.spec.ts', 'status': 'failed', 'leaf': 'REQ-1-1'},
                 'REQ-1-2.spec.ts::REQ-1-2 #1: z': {'file': 'REQ-1-2.spec.ts', 'status': 'failed', 'leaf': 'REQ-1-2'}}
        loop.runner = _FakeRunner([{'status': 'executed', 'tests': tests, 'seconds': 3}])
        loop._legacy_baseline()
        self.assertEqual(sorted(loop.legacy_specs), ['REQ-1-1.spec.ts'])
        self.assertEqual(loop.latest['REQ-1-1.spec.ts::REQ-1-1 #1: x'], 'passed')
        self.assertIn('REQ-1-1.spec.ts::REQ-1-1 #2: y', loop.disputed)
        self.assertNotIn('REQ-1-2.spec.ts::REQ-1-2 #1: z', loop.latest)
        self.assertEqual(status['acceptance']['legacy']['guard_files'], 1)
        self.assertIn('REQ-1-1', loop.leaf_nodes, 'earlier requirement text is available to repair prompts')
        self.assertEqual(loop._legacy_guard(), {'REQ-1-1.spec.ts': 'a'})
        self.assertEqual(loop._all_specs(), {'REQ-1-1.spec.ts': 'a'})
        loop.deadline = time.monotonic() + 100
        self.assertEqual(loop._legacy_guard(), {}, 'late in the run only full runs include the guards')


class TransportTests(unittest.TestCase):
    def setUp(self):
        for key in model_transport.LEARNED:
            model_transport.LEARNED[key] = None if key == "max_tokens_cap" else False

    def test_time_capped_attempt_is_retried_at_lower_effort(self):
        now = [0.0]

        def clock():
            return now[0]

        def slow():
            for _ in range(10):
                now[0] += 40
                yield _Chunk(reasoning='r' * 100)
        client = Client([slow(), ok_stream()])
        events = []
        value = model_transport.request_json(client, "m", "architecture", "P", lambda d: d, events, [], lambda: None,
                                             1e9, sleep=lambda s: None, clock=clock, reasoning="high", attempt_timeout=100)
        self.assertEqual(value, {"ok": 1})
        failed = [e for e in events if e.get('state') == 'request_failed']
        self.assertIn('exceeded', failed[0]['error_detail'])
        self.assertEqual(failed[0]['retry_reasoning'], 'low')
        self.assertGreater(failed[0]['partial_reasoning_chars'], 0)
        self.assertEqual(client.calls[1]["reasoning_effort"], "low")

    def test_architecture_call_gets_a_longer_attempt(self):
        seen = {}

        def call(stage, prompt, validator, seconds=650, **options):
            seen.update(stage=stage, seconds=seconds, prompt=prompt, **options)
            return None
        root = Path(tempfile.mkdtemp())
        (root / 'frontend/src').mkdir(parents=True)
        (root / 'frontend/src/App.tsx').write_text('<Route path="/login" />')
        memory = {'inherited': True, 'previous_plan': {'architecture': 'OLD PLAN'}, 'previous_outline': '- REQ-1: Old',
                  'prerequisites': [{'id': 'REQ-1', 'name': 'Old', 'description': ''}]}
        plan, _ = ga.plan_architecture(root, doc(['REQ-2']), call, {}, memory=memory)
        self.assertEqual(seen['attempt_timeout'], 600)
        self.assertGreaterEqual(seen['seconds'], 1200)
        self.assertIn('THE APPLICATION ALREADY EXISTS', seen['prompt'])
        self.assertIn('OLD PLAN', seen['prompt'])
        self.assertIn('/login', seen['prompt'])
        self.assertIn('EARLIER-STAGE PREREQUISITES', seen['prompt'])
        self.assertTrue(plan.get('fallback'))


class RevertKeepsCacheOrderTests(unittest.TestCase):
    def test_restored_file_gets_its_old_mtime(self):
        root = Path(tempfile.mkdtemp())
        path = root / 'frontend/src/A.tsx'
        path.parent.mkdir(parents=True)
        path.write_text('old')
        stamp = time.time() - 5000
        os.utime(path, (stamp, stamp))
        before = ga.source_snapshot(root)
        old_ns = path.stat().st_mtime_ns
        path.write_text('new')
        ga.restore_sources(root, before)
        self.assertEqual(path.read_text(), 'old')
        self.assertEqual(path.stat().st_mtime_ns, old_ns)


if __name__ == '__main__':
    unittest.main()
