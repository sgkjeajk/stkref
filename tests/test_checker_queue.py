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
    return {n: {'id': n, 'created_at': str(n), 'status': 'in_progress', 'display_title': 'STKREF single-check '+str(n)}
            for n in range(1, count + 1)}


class QueueTests(unittest.TestCase):
    def execute(self, count=0, enabled=True, cycles=10, threshold=5, batch=10, mode='queue', **env):
        with tempfile.NamedTemporaryFile() as out:
            environment = {'QUEUE_MODE': mode, 'REPO': 'owner/repo', 'GH_TOKEN': 'test',
                           'GITHUB_RUN_ID': '1', 'GITHUB_OUTPUT': out.name, **env}
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(queue, 'settings', return_value=(enabled, 1, threshold, batch)), \
                 patch.object(queue, 'active_runs', return_value=runs(count)), \
                 patch.object(queue, 'dispatch') as dispatch, \
                 contextlib.redirect_stdout(io.StringIO()):
                queue.main()
                return dispatch.call_args_list, Path(out.name).read_text()

    def test_empty_pool_queues_ten_single_check_programs(self):
        calls, _ = self.execute()
        self.assertEqual(len(calls), 10)
        self.assertTrue(all(call.args[2] == 1 for call in calls))

    def test_every_count_zero_through_five_adds_exactly_ten(self):
        for count in range(6):
            with self.subTest(count=count):
                self.assertEqual(len(self.execute(count=count)[0]), 10)

    def test_above_threshold_never_dispatches(self):
        for count in (6, 10, 15, 16):
            self.assertEqual(self.execute(count=count)[0], [])

    def test_disabled_never_replenishes(self):
        self.assertEqual(self.execute(enabled=False)[0], [])

    def test_single_cycle_setting_still_replenishes(self):
        calls, _ = self.execute(cycles=1, batch=2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c.args[2] == 1 for c in calls))

    def test_legacy_guard_stops_old_parallel_workflows(self):
        for count in (0, 5, 15):
            self.assertEqual(self.execute(count=count, mode='guard')[1], 'allowed=false\n')

    def test_legacy_guard_never_dispatches_children(self):
        self.assertEqual(self.execute(mode='guard')[0], [])

    def test_unrecognised_mode_fails(self):
        with self.assertRaises(ValueError):
            self.execute(mode='typo')

    def test_failed_dispatch_stops_instead_of_claiming_success(self):
        with patch.object(queue, 'dispatch', side_effect=SystemExit('denied')), \
             patch.object(queue, 'settings', return_value=(True, 10, 5, 10)), \
             patch.object(queue, 'active_runs', return_value={}), \
             patch.dict(os.environ, {'QUEUE_MODE':'queue','REPO':'owner/repo','GH_TOKEN':'test'}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                queue.main()

    def test_pending_states_pagination_and_deduplication(self):
        urls = []
        def api(method, url, token):
            urls.append(url)
            if 'status=queued&' in url and 'page=1' == url[-6:]:
                return {'workflow_runs': [{'id': n, 'display_title': 'STKREF single-check '+str(n)} for n in range(100)]}
            if 'status=queued&' in url:
                return {'workflow_runs': [{'id': 100, 'display_title': 'STKREF single-check 100'}]}
            if 'status=in_progress&' in url:
                return {'workflow_runs': [{'id': 0, 'display_title': 'STKREF single-check 0'}]}
            return {'workflow_runs': []}
        with patch.object(queue, 'gh', side_effect=api):
            result = queue.active_runs('owner/repo', 'task.yml', 'test')
        self.assertEqual(len(result), 101)
        self.assertTrue(any('page=2' in u for u in urls))
        for state in ('waiting', 'pending', 'requested'):
            self.assertTrue(any('status='+state+'&' in u for u in urls))

    def test_old_parallel_runs_do_not_block_the_new_serial_queue(self):
        def api(method, url, token):
            return {'workflow_runs': [{'id': 99, 'display_title': 'Task'}]}
        with patch.object(queue, 'gh', side_effect=api):
            self.assertEqual(queue.active_runs('owner/repo', 'task.yml', 'test'), {})

    def test_dispatch_enforces_one_check_and_preserves_main_branch(self):
        with patch.object(queue, 'gh') as api:
            queue.dispatch('owner/repo', 'task.yml', 10, 'test', 'request')
        args = api.call_args.args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[3]['ref'], 'main')
        self.assertEqual(args[3]['inputs']['checker_cycles'], '1')
        self.assertTrue(args[3]['inputs']['automatic_topup'])

    def test_settings_disable_when_all_categories_off(self):
        config = {'priceChecker': {'enabled': True, 'cycles': 10, 'maxActiveCheckers': 5,
                                  'categories': dict.fromkeys(('ETF','Stock','Fund'), False)}}
        with patch('builtins.open', unittest.mock.mock_open(read_data=json.dumps(config))):
            self.assertEqual(queue.settings(), (False, 1, 5, 10))

    def test_settings_category_selection_and_custom_limit(self):
        config = {'priceChecker': {'enabled': True, 'cycles': 7, 'maxActiveCheckers': 3, 'refillThreshold': 2, 'topUpJobs': 7,
                                  'categories': {'ETF':True,'Stock':False,'Fund':False}}}
        with patch('builtins.open', unittest.mock.mock_open(read_data=json.dumps(config))):
            self.assertEqual(queue.settings(), (True, 1, 2, 7))

    def test_five_remaining_adds_ten_then_fifteen_adds_none(self):
        calls, _ = self.execute(count=5)
        self.assertEqual(len(calls), 10)
        self.assertEqual(self.execute(count=15)[0], [])

    def test_program_itself_checks_queue_before_work_and_after_any_outcome(self):
        root = Path(__file__).resolve().parents[1]
        worker = (root / '.github/workflows/task.yml').read_text()
        start = worker.index('name: Check saved queue settings at program startup')
        tests = worker.index('name: Test checker, queue and copy protections')
        check = worker.index('run: python scripts/checker_once.py')
        finish = worker.index('name: Check saved queue settings after every outcome')
        self.assertLess(start, tests)
        self.assertLess(tests, check)
        self.assertLess(check, finish)
        self.assertIn('if: always()', worker[finish:])
        self.assertEqual(worker.count('QUEUE_MODE: queue'),2)
        self.assertEqual(worker.count('python scripts/checker_queue.py'),2)
        self.assertIn('GH_TOKEN: ${{ github.token }}',worker[start:tests])
        self.assertIn('GH_TOKEN: ${{ github.token }}',worker[finish:])
        self.assertIn('group: stkref-price-checker',worker)
        self.assertIn('queue: max',worker)
        self.assertNotIn('seq 1',worker)

    def test_bootstrap_has_no_recursive_completion_dependency(self):
        controller = (Path(__file__).resolve().parents[1]/'.github/workflows/checker-watchdog.yml').read_text()
        self.assertNotIn('  workflow_run:',controller)
        self.assertIn('QUEUE_MODE: bootstrap',controller)
        self.assertIn('group: stkref-replenishment',controller)
        self.assertIn('queue: max',controller)

    def test_bootstrap_starts_exactly_one_program_only_when_pool_is_empty(self):
        self.assertEqual(len(self.execute(count=0,mode='bootstrap')[0]),1)
        for count in (1,5,6,15):
            self.assertEqual(self.execute(count=count,mode='bootstrap')[0],[])

    def test_bootstrap_cannot_race_worker_into_a_duplicate_full_batch(self):
        self.assertEqual(self.execute(count=5,mode='bootstrap')[0],[])
        self.assertEqual(len(self.execute(count=5,mode='queue')[0]),10)
        self.assertEqual(self.execute(count=15,mode='bootstrap')[0],[])
        self.assertEqual(self.execute(count=15,mode='queue')[0],[])

    def test_disabled_bootstrap_never_seeds(self):
        self.assertEqual(self.execute(count=0,mode='bootstrap',enabled=False)[0],[])

    def test_many_programs_keep_the_queue_alive_without_workflow_run_events(self):
        active=1
        batches=0
        for number in range(60):
            # These represent the actual startup and always-run finish steps;
            # the checker outcome deliberately has no effect on replenishment.
            for event in ('program_start','program_finish'):
                calls,_=self.execute(count=active,CHECKER_EVENT=event)
                if calls:
                    self.assertLessEqual(active,5)
                    self.assertEqual(len(calls),10)
                    batches+=1
                    active+=10
            active-=1
            self.assertGreater(active,0)
        self.assertGreater(batches,3)



if __name__ == '__main__':
    unittest.main()
