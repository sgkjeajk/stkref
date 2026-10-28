import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import checker_queue as queue


def runs(count):
    return {n: {'id': n, 'created_at': str(n), 'status': 'in_progress'}
            for n in range(1, count + 1)}


class QueueTests(unittest.TestCase):
    def execute(self, count=0, enabled=True, cycles=10, limit=5, mode='watchdog', **env):
        with tempfile.NamedTemporaryFile() as out:
            environment = {'QUEUE_MODE': mode, 'REPO': 'owner/repo', 'GH_TOKEN': 'test',
                           'GITHUB_RUN_ID': '1', 'GITHUB_OUTPUT': out.name, **env}
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(queue, 'settings', return_value=(enabled, cycles, limit)), \
                 patch.object(queue, 'active_runs', return_value=runs(count)), \
                 patch.object(queue, 'dispatch') as dispatch, \
                 contextlib.redirect_stdout(io.StringIO()):
                queue.main()
                return dispatch.call_args_list, Path(out.name).read_text()

    def test_empty_pool_fills_five_slots_with_ten_cycles_each(self):
        calls, _ = self.execute()
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call.args[2] == 10 for call in calls))

    def test_only_missing_slots_are_filled(self):
        for count in range(6):
            with self.subTest(count=count):
                self.assertEqual(len(self.execute(count=count)[0]), 5-count)

    def test_at_or_above_limit_never_dispatches(self):
        for count in (5, 6, 10):
            self.assertEqual(self.execute(count=count)[0], [])

    def test_disabled_never_replenishes(self):
        self.assertEqual(self.execute(enabled=False)[0], [])

    def test_single_cycle_setting_still_replenishes(self):
        calls, _ = self.execute(cycles=1, limit=2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c.args[2] == 1 for c in calls))

    def test_guard_keeps_five_oldest_when_six_runs_overlap(self):
        allowed = [queue.admitted(runs(6), n, 5) for n in range(1, 7)]
        self.assertEqual(allowed, [True]*5 + [False])

    def test_guard_includes_current_run_when_api_visibility_lags(self):
        self.assertTrue(queue.admitted({}, 99, 5))
        self.assertFalse(queue.admitted(runs(5), 99, 5))

    def test_guard_existing_worker_not_rejected_by_new_arrivals(self):
        _, output = self.execute(count=9, mode='guard')
        self.assertEqual(output, 'allowed=true\n')

    def test_guard_excess_newcomer_rejected(self):
        _, output = self.execute(count=6, mode='guard', GITHUB_RUN_ID='6')
        self.assertEqual(output, 'allowed=false\n')

    def test_disabled_guard_and_manual_override(self):
        self.assertEqual(self.execute(enabled=False, mode='guard')[1], 'allowed=false\n')
        self.assertEqual(self.execute(enabled=False, mode='guard', FORCE_PRICE_CHECK='true')[1], 'allowed=true\n')
        self.assertEqual(self.execute(enabled=False, mode='guard', REFRESH_ATH='true')[1], 'allowed=true\n')

    def test_guard_never_dispatches_children(self):
        self.assertEqual(self.execute(mode='guard')[0], [])

    def test_unrecognised_mode_fails(self):
        with self.assertRaises(ValueError):
            self.execute(mode='typo')

    def test_failed_dispatch_stops_instead_of_claiming_success(self):
        with patch.object(queue, 'dispatch', side_effect=SystemExit('denied')), \
             patch.object(queue, 'settings', return_value=(True, 10, 5)), \
             patch.object(queue, 'active_runs', return_value={}), \
             patch.dict(os.environ, {'QUEUE_MODE':'watchdog','REPO':'owner/repo','GH_TOKEN':'test'}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                queue.main()

    def test_pending_states_pagination_and_deduplication(self):
        urls = []
        def api(method, url, token):
            urls.append(url)
            if 'status=queued&' in url and 'page=1' == url[-6:]:
                return {'workflow_runs': [{'id': n} for n in range(100)]}
            if 'status=queued&' in url:
                return {'workflow_runs': [{'id': 100}]}
            if 'status=in_progress&' in url:
                return {'workflow_runs': [{'id': 0}]}
            return {'workflow_runs': []}
        with patch.object(queue, 'gh', side_effect=api):
            result = queue.active_runs('owner/repo', 'task.yml', 'test')
        self.assertEqual(len(result), 101)
        self.assertTrue(any('page=2' in u for u in urls))
        for state in ('waiting', 'pending', 'requested'):
            self.assertTrue(any('status='+state+'&' in u for u in urls))

    def test_dispatch_preserves_saved_cycles_and_main_branch(self):
        with patch.object(queue, 'gh') as api:
            queue.dispatch('owner/repo', 'task.yml', 10, 'test', 'request')
        args = api.call_args.args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[3]['ref'], 'main')
        self.assertEqual(args[3]['inputs']['checker_cycles'], '10')
        self.assertTrue(args[3]['inputs']['automatic_topup'])

    def test_settings_disable_when_all_categories_off(self):
        config = {'priceChecker': {'enabled': True, 'cycles': 10, 'maxActiveCheckers': 5,
                                  'categories': dict.fromkeys(('ETF','Stock','Fund'), False)}}
        with patch('builtins.open', unittest.mock.mock_open(read_data=json.dumps(config))):
            self.assertEqual(queue.settings(), (False, 10, 5))

    def test_settings_category_selection_and_custom_limit(self):
        config = {'priceChecker': {'enabled': True, 'cycles': 7, 'maxActiveCheckers': 3,
                                  'categories': {'ETF':True,'Stock':False,'Fund':False}}}
        with patch('builtins.open', unittest.mock.mock_open(read_data=json.dumps(config))):
            self.assertEqual(queue.settings(), (True, 7, 3))

    def test_completed_worker_replacement_then_no_duplicate_refill(self):
        calls, _ = self.execute(count=4)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.execute(count=5)[0], [])

    def test_workflow_completion_recovery_covers_failures(self):
        root = Path(__file__).resolve().parents[1]
        controller = (root / '.github/workflows/checker-watchdog.yml').read_text()
        worker = (root / '.github/workflows/task.yml').read_text()
        self.assertIn('workflows: [Task]', controller)
        self.assertIn('types: [completed]', controller)
        self.assertNotIn('conclusion ==', controller)
        self.assertIn('cancel-in-progress: false', controller)
        self.assertNotIn('group: stkref-checker-queue', worker)
        self.assertIn('group: stkref-replenishment', controller)
        self.assertNotIn('Top up the active checker queue', worker)
        self.assertNotIn('Hand off to the next checker', worker)
        self.assertNotIn('QUEUE_MODE: watchdog', worker)
        self.assertLess(worker.index('python scripts/copy_market_data.py'), worker.index('sleep "$sleep_seconds"'))


if __name__ == '__main__':
    unittest.main()
