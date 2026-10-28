import datetime as dt
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import checker_once as once
import github_market_update as update

AT = dt.datetime(2026, 10, 29, 0, 0, tzinfo=dt.timezone.utc)
CONFIG = {'revision': 45, 'priceChecker': {'enabled': True, 'intervalMinutes': 6,
          'categories': {'ETF':True,'Stock':True,'Fund':False}},
          'instruments': [{'ticker':'AAPL','enabled':True,'type':'Stock'}]}


def snapshot(age):
    stamp = (AT-dt.timedelta(seconds=age)).isoformat()
    return {'last_check_completed_at':stamp, 'generated_at':stamp,
            'config_revision':45,
            'instruments':[{'ticker':'AAPL','price_retrieved_at':stamp}]}


class SingleCheckTests(unittest.TestCase):
    def test_six_minute_boundary(self):
        self.assertEqual(once.seconds_to_wait(CONFIG, snapshot(0), AT), 360)
        self.assertEqual(once.seconds_to_wait(CONFIG, snapshot(359), AT), 1)
        self.assertEqual(once.seconds_to_wait(CONFIG, snapshot(360), AT), 0)
        self.assertEqual(once.seconds_to_wait(CONFIG, snapshot(1000), AT), 0)

    def test_interval_comes_from_settings(self):
        for minutes in (5, 6, 8, 15, 30):
            cfg = {**CONFIG, 'priceChecker': {**CONFIG['priceChecker'], 'intervalMinutes':minutes}}
            self.assertEqual(once.seconds_to_wait(cfg, snapshot(0), AT), minutes*60)

    def test_missing_or_invalid_timestamp_starts_without_deadlock(self):
        for data in ({}, {'generated_at':'bad'}, {'generated_at':123}, {'generated_at':'2026-10-29T00:00:00'}):
            self.assertEqual(once.seconds_to_wait(CONFIG, data, AT), 0)

    def test_future_timestamp_does_not_wait_forever(self):
        self.assertEqual(once.seconds_to_wait(CONFIG, snapshot(-99999), AT), 360)

    def test_waiter_observes_disable_change_before_retrieving(self):
        disabled = {**CONFIG, 'priceChecker':{**CONFIG['priceChecker'],'enabled':False}}
        reads = Mock(side_effect=[(CONFIG, snapshot(0)), (disabled, snapshot(0))])
        pause = Mock()
        self.assertFalse(once.wait_until_due(sync_state=lambda:None,read=reads,pause=pause,clock=lambda:AT))
        pause.assert_called_once_with(30)

    def test_waiter_uses_updated_interval(self):
        cfg = {**CONFIG, 'priceChecker':{**CONFIG['priceChecker'],'intervalMinutes':5}}
        reads = Mock(side_effect=[(CONFIG,snapshot(300)),(cfg,snapshot(300))])
        pause=Mock()
        self.assertTrue(once.wait_until_due(sync_state=lambda:None,read=reads,pause=pause,clock=lambda:AT))
        pause.assert_called_once_with(30)

    def test_manual_request_still_respects_interval(self):
        reads=Mock(side_effect=[(CONFIG,snapshot(359)),(CONFIG,snapshot(360))])
        pause=Mock()
        self.assertTrue(once.wait_until_due(sync_state=lambda:None,read=reads,pause=pause,clock=lambda:AT,forced=True))
        pause.assert_called_once_with(1)

    def test_no_categories_is_disabled(self):
        cfg={'priceChecker':{'categories':dict.fromkeys(('ETF','Stock','Fund'),False)}}
        self.assertFalse(once.enabled(cfg))

    def run_program(self, returncodes):
        commands=[]
        with patch.object(once,'wait_until_due',return_value=True), \
             patch.object(once,'read_state',return_value=(CONFIG,snapshot(360))), \
             patch.object(once,'record_delivery',return_value='stamp'), \
             patch.object(once,'mark_delivered'), \
             patch.object(once,'command',side_effect=lambda *args,**kwargs:commands.append(args)), \
             patch.object(once.subprocess,'run',side_effect=[Mock(returncode=x) for x in returncodes]):
            once.main()
        return commands

    def test_one_retrieval_then_publication_then_direct_copy_and_exit(self):
        commands=self.run_program([1,0])
        names=[args[1] for args in commands if args[0]==sys.executable]
        self.assertEqual(names,['scripts/github_market_update.py','scripts/copy_market_data.py'])
        self.assertLess(commands.index((sys.executable,'scripts/github_market_update.py')),
                        commands.index(('git','commit','-m','Publish one completed STKREF price check')))

    def test_unchanged_source_still_attempts_direct_delivery(self):
        commands=self.run_program([0])
        self.assertIn((sys.executable,'scripts/copy_market_data.py'),commands)

    def test_conflicting_push_regenerates_but_still_only_one_final_copy(self):
        commands=self.run_program([1,1,1,0])
        self.assertEqual(commands.count((sys.executable,'scripts/github_market_update.py')),2)
        self.assertEqual(commands.count((sys.executable,'scripts/copy_market_data.py')),1)

    def test_three_failed_publications_fail_without_copying_unpublished_data(self):
        commands=[]
        with patch.object(once,'wait_until_due',return_value=True), \
             patch.object(once,'read_state',return_value=(CONFIG,snapshot(360))), \
             patch.object(once,'record_delivery',return_value='stamp'), \
             patch.object(once,'mark_delivered'), \
             patch.object(once,'command',side_effect=lambda *args,**kwargs:commands.append(args)), \
             patch.object(once.subprocess,'run',return_value=Mock(returncode=1)):
            with self.assertRaises(RuntimeError):once.main()
        self.assertNotIn((sys.executable,'scripts/copy_market_data.py'),commands)

    def test_copy_failure_is_visible_as_job_failure(self):
        def command(*args,**kwargs):
            if args == (sys.executable,'scripts/copy_market_data.py'):
                raise subprocess.CalledProcessError(1,args)
        with patch.object(once,'wait_until_due',return_value=True), \
             patch.object(once,'read_state',return_value=(CONFIG,snapshot(360))), \
             patch.object(once,'record_delivery',return_value='stamp'), \
             patch.object(once,'mark_delivered'), \
             patch.object(once,'command',side_effect=command), \
             patch.object(once.subprocess,'run',side_effect=[Mock(returncode=1),Mock(returncode=0)]):
            with self.assertRaises(subprocess.CalledProcessError):once.main()

    def test_disabled_program_never_retrieves_or_copies(self):
        with patch.object(once,'wait_until_due',return_value=False),patch.object(once,'command') as command:
            self.assertEqual(once.main(),0)
        command.assert_not_called()

    def test_freshness_and_heartbeat_use_six_not_ten_minutes(self):
        self.assertEqual(update.configured_interval_seconds(CONFIG),360)
        self.assertTrue(update.prices_are_fresh(CONFIG,snapshot(359),AT))
        self.assertFalse(update.prices_are_fresh(CONFIG,snapshot(360),AT))
        self.assertFalse(update.recent_check(snapshot(360),AT,update.configured_interval_seconds(CONFIG)))

    def test_failed_copy_bypasses_six_minute_wait_for_next_program(self):
        data = {**snapshot(1), '_copy_pending':True}
        self.assertEqual(once.seconds_to_wait(CONFIG,data,AT),0)

    def test_pending_marker_is_read_only_for_its_matching_source_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            (root/'data/stock-tickers.json').write_text(json.dumps(CONFIG))
            data=snapshot(1);(root/'market-data.json').write_text(json.dumps(data))
            delivery={'status':'pending','source_market_generated_at':data['generated_at']}
            path=root/'data/checker-delivery.json';path.write_text(json.dumps(delivery))
            with patch.object(once,'ROOT',root):
                self.assertTrue(once.read_state()[1]['_copy_pending'])
                delivery['status']='completed';path.write_text(json.dumps(delivery))
                self.assertFalse(once.read_state()[1]['_copy_pending'])
                delivery.update(status='pending',source_market_generated_at='older')
                path.write_text(json.dumps(delivery))
                self.assertFalse(once.read_state()[1]['_copy_pending'])

    def test_failed_copy_leaves_persisted_pending_marker_and_no_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            (root/'data/stock-tickers.json').write_text(json.dumps(CONFIG))
            (root/'market-data.json').write_text(json.dumps(snapshot(360)))
            def command(*args,**kwargs):
                if args==(sys.executable,'scripts/copy_market_data.py'):
                    raise subprocess.CalledProcessError(1,args)
            with patch.object(once,'ROOT',root), \
                 patch.object(once,'wait_until_due',return_value=True), \
                 patch.object(once,'command',side_effect=command), \
                 patch.object(once,'mark_delivered') as acknowledge, \
                 patch.object(once.subprocess,'run',side_effect=[Mock(returncode=1),Mock(returncode=0)]):
                with self.assertRaises(subprocess.CalledProcessError):once.main()
                self.assertTrue(once.read_state()[1]['_copy_pending'])
                acknowledge.assert_not_called()

    def test_successful_delivery_clears_pending_marker_after_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            (root/'data/stock-tickers.json').write_text(json.dumps(CONFIG))
            (root/'market-data.json').write_text(json.dumps(snapshot(1)))
            with patch.object(once,'ROOT',root),patch.object(once,'sync'), \
                 patch.object(once,'command'), \
                 patch.object(once.subprocess,'run',side_effect=[Mock(returncode=1),Mock(returncode=0)]):
                stamp=once.record_delivery('pending')
                self.assertTrue(once.read_state()[1]['_copy_pending'])
                once.mark_delivered(stamp)
                self.assertFalse(once.read_state()[1]['_copy_pending'])
                self.assertEqual(once.seconds_to_wait(CONFIG,once.read_state()[1],AT),359)

    def test_acknowledgement_cannot_clear_a_different_newer_snapshot(self):
        with patch.object(once,'sync'),patch.object(once,'read_state',return_value=(CONFIG,snapshot(1))):
            with self.assertRaises(RuntimeError):once.mark_delivered('different')

    def test_pending_delivery_forces_new_retrieval_instead_of_fresh_price_skip(self):
        captured=[]
        def command(*args,**kwargs):
            if args==(sys.executable,'scripts/github_market_update.py'):
                captured.append(kwargs['env']['FORCE_PRICE_CHECK'])
        with patch.object(once,'wait_until_due',return_value=True), \
             patch.object(once,'read_state',return_value=(CONFIG,{**snapshot(1),'_copy_pending':True})), \
             patch.object(once,'record_delivery',return_value='stamp'), \
             patch.object(once,'mark_delivered'), \
             patch.object(once,'command',side_effect=command), \
             patch.object(once.subprocess,'run',side_effect=[Mock(returncode=1),Mock(returncode=0)]):
            once.main()
        self.assertEqual(captured,['true'])

    def test_legacy_loop_program_exits_before_provider_access(self):
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'true','CHECKER_SINGLE_PASS':'false'}), \
             patch.object(update,'refresh',side_effect=AssertionError('Must not retrieve')):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(Path(update.__file__)),run_name='__main__')
        self.assertEqual(result.exception.code,0)


if __name__=='__main__':unittest.main()
