import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import checker_once as program


def config(cycles=10, enabled=True):
    return {'priceChecker':{'cycles':cycles,'enabled':enabled,'intervalMinutes':6,
                            'categories':{'ETF':True,'Stock':True,'Fund':False}}}


class RepeatedProgramTests(unittest.TestCase):
    def exercise(self, cycles=10, fail_copy=None):
        events=[]
        copies=0
        def command(*args,**kwargs):
            nonlocal copies
            if args[0]==sys.executable:
                events.append(args[1])
                if args[1]=='scripts/copy_market_data.py':
                    copies+=1
                    if copies==fail_copy:
                        raise subprocess.CalledProcessError(1,args)
        with patch.object(program,'sync'), \
             patch.object(program,'read_state',return_value=(config(cycles),{})), \
             patch.object(program,'wait_until_due',return_value=True) as waiter, \
             patch.object(program,'record_delivery',return_value='stamp'), \
             patch.object(program,'mark_delivered',side_effect=lambda _:events.append('ack')), \
             patch.object(program,'command',side_effect=command), \
             patch.object(program.subprocess,'run',return_value=Mock(returncode=0)), \
             contextlib.redirect_stdout(io.StringIO()):
            if fail_copy:
                with self.assertRaises(subprocess.CalledProcessError):program.main()
            else:
                self.assertEqual(program.main(),0)
            return events,waiter.call_count

    def test_ten_checks_each_delivered_before_the_next_check(self):
        events,waits=self.exercise()
        self.assertEqual(waits,10)
        self.assertEqual(events,['scripts/github_market_update.py','scripts/copy_market_data.py',
                                 'ack','scripts/checker_queue.py']*10)

    def test_saved_cycle_count_controls_program_not_legacy_dispatch_input(self):
        with patch.dict(os.environ,{'CHECKER_CYCLES':'1'}):
            events,waits=self.exercise(cycles=3)
        self.assertEqual(waits,3)
        self.assertEqual(events.count('scripts/copy_market_data.py'),3)

    def test_copy_failure_stops_loop_and_still_checks_queue(self):
        events,waits=self.exercise(fail_copy=4)
        self.assertEqual(waits,4)
        self.assertEqual(events.count('ack'),3)
        self.assertEqual(events[-2:],['scripts/copy_market_data.py','scripts/checker_queue.py'])
        self.assertEqual(events.count('scripts/checker_queue.py'),4)

    def test_disable_during_program_stops_further_checks(self):
        with patch.object(program,'sync'), \
             patch.object(program,'read_state',side_effect=[(config(),{}),(config(),{}),(config(enabled=False),{})]), \
             patch.object(program,'check_once') as check,patch.object(program,'command'), \
             contextlib.redirect_stdout(io.StringIO()):
            program.main()
        self.assertEqual(check.call_count,1)

    def test_manual_forcing_and_history_apply_only_to_first_check(self):
        seen=[]
        with patch.dict(os.environ,{'FORCE_PRICE_CHECK':'true','REFRESH_ATH':'true','TRIGGER_EVENT':'workflow_dispatch'}), \
             patch.object(program,'sync'),patch.object(program,'read_state',return_value=(config(3),{})), \
             patch.object(program,'check_once',side_effect=lambda:seen.append(tuple(os.environ[k] for k in ('FORCE_PRICE_CHECK','REFRESH_ATH','TRIGGER_EVENT')))), \
             patch.object(program,'command'),contextlib.redirect_stdout(io.StringIO()):
            program.main()
            self.assertEqual(os.environ['REFRESH_ATH'],'true')
        self.assertEqual(seen,[('true','true','workflow_dispatch'),('false','false','schedule'),('false','false','schedule')])

    def test_new_and_migrating_programs_are_both_counted(self):
        import checker_queue
        with patch.object(checker_queue,'gh',return_value={'workflow_runs':[
            {'id':1,'display_title':'STKREF single-check 1'},
            {'id':2,'display_title':'STKREF price checker 2'},
            {'id':3,'display_title':'Task'}]}):
            self.assertEqual(set(checker_queue.active_runs('o/r','task.yml','t')),{1,2})
