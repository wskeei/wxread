import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import daily_read as daily


class DailyTests(unittest.TestCase):
    def test_beijing_day_and_start_window(self):
        before = datetime(2026, 11, 16, 1, 6, tzinfo=daily.TZ)
        due = before.replace(minute=7)
        self.assertFalse(daily.decision(before, None, 120, True)[0])
        self.assertTrue(daily.decision(due, None, 120, True)[0])
        self.assertTrue(daily.decision(before, None, 120, False)[0])

    def test_completed_day_skips_and_partial_resumes(self):
        current = datetime(2026, 11, 16, 6, tzinfo=daily.TZ)
        self.assertFalse(daily.decision(current, {'completed': 120}, 120, True)[0])
        self.assertTrue(daily.decision(current, {'completed': 30}, 120, True)[0])
        self.assertTrue(daily.decision(current, {'completed': 120}, 150, True)[0])

    def test_checkpoint_does_not_lose_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'progress.json'
            self.assertEqual(daily.checkpoint(path, 10), 10)
            path.write_text('{"completed": 12}')
            self.assertEqual(daily.checkpoint(path, 10), 12)
            path.write_text('{"completed": 9}')
            with self.assertRaises(ValueError):
                daily.checkpoint(path, 10)

    def test_retry_resumes_partial_read_and_persists_completion(self):
        current = datetime(2026, 11, 16, 2, tzinfo=daily.TZ)
        api = MagicMock()
        state = {'date': '2026-11-16', 'completed': 10}
        offsets = []
        with tempfile.TemporaryDirectory() as folder:
            def launch(_command, env):
                offsets.append(env['READ_START_INDEX'])
                count = 20 if len(offsets) == 1 else 120
                Path(env['READ_PROGRESS_FILE']).write_text(json.dumps({'completed': count}))
                child = MagicMock()
                child.poll.return_value = 1 if count == 20 else 0
                return child
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'RUNNER_TEMP': folder}), \
                 patch.object(daily, 'now', return_value=current), \
                 patch.object(daily.subprocess, 'Popen', side_effect=launch), \
                 patch.object(daily.time, 'sleep'):
                daily.run_daily(api, state, 120)
        self.assertEqual(offsets, ['10', '20'])
        self.assertEqual(state['completed'], 120)
        self.assertEqual(api.save.call_count, 3)

    def test_failed_attempts_remain_failed_for_next_check(self):
        current = datetime(2026, 11, 16, 2, tzinfo=daily.TZ)
        child = MagicMock()
        child.poll.return_value = 1
        api = MagicMock()
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'RUNNER_TEMP': folder}), \
             patch.object(daily, 'now', return_value=current), \
             patch.object(daily.subprocess, 'Popen', return_value=child) as launch, \
             patch.object(daily.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                daily.run_daily(api, {'date': '2026-11-16', 'completed': 0}, 120)
            self.assertEqual(launch.call_count, 3)

    def test_queued_yesterday_job_does_not_read_today(self):
        with patch.dict(os.environ, {'READ_DAY': '2026-11-15', 'READ_NUM': '120'}), \
             patch.object(daily, 'now', return_value=datetime(2026, 11, 16, 2, tzinfo=daily.TZ)), \
             patch('sys.argv', ['daily_read.py', 'run']):
            with self.assertRaisesRegex(RuntimeError, 'different day'):
                daily.main()

    def test_legacy_success_requires_real_read_step(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'GH_TOKEN': 'test',
                                    'GITHUB_RUN_ID': '99', 'GITHUB_REF_NAME': 'main'}):
            api = daily.GitHub()
        run = {'id': 1, 'html_url': 'https://example.test/run'}
        api.api = MagicMock(side_effect=[
            {'workflow_runs': [run]},
            {'jobs': [{'steps': [{'name': 'Run deployment script', 'conclusion': 'success',
                                 'completed_at': '2026-11-15T17:50:00Z'}]}]},
        ])
        self.assertEqual(api.legacy_success(datetime(2026, 11, 16).date()), run['html_url'])
        api.api = MagicMock(side_effect=[
            {'workflow_runs': [run]},
            {'jobs': [{'steps': [{'name': 'Run deployment script', 'conclusion': 'skipped'}]}]},
        ])
        self.assertIsNone(api.legacy_success(datetime(2026, 11, 16).date()))

    def test_durable_state_refuses_progress_regression(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'GH_TOKEN': 'test'}):
            api = daily.GitHub()
        api.api = MagicMock()
        api.load = MagicMock(return_value=({'completed': 120}, 'sha'))
        with self.assertRaisesRegex(RuntimeError, 'newer'):
            api.save({'date': '2026-11-16', 'completed': 30})

    def test_storage_failure_prevents_reading(self):
        api = MagicMock()
        api.save.side_effect = RuntimeError('storage unavailable')
        current = datetime(2026, 11, 16, 2, tzinfo=daily.TZ)
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'RUNNER_TEMP': folder}), \
             patch.object(daily, 'now', return_value=current), \
             patch.object(daily.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'storage unavailable'):
                daily.run_daily(api, {'date': '2026-11-16', 'completed': 0}, 120)
            launch.assert_not_called()

    def test_check_only_does_not_write_migrated_state(self):
        api = MagicMock()
        api.load.return_value = (None, None)
        api.legacy_success.return_value = 'https://example.test/old-success'
        with patch.dict(os.environ, {'CHECK_ONLY': 'true', 'READ_NUM': '120',
                                    'READ_DAY': '2026-11-16', 'GITHUB_EVENT_NAME': 'workflow_dispatch'}), \
             patch.object(daily, 'now', return_value=datetime(2026, 11, 16, 2, tzinfo=daily.TZ)), \
             patch.object(daily, 'GitHub', return_value=api), \
             patch('sys.argv', ['daily_read.py', 'check']):
            daily.main()
        api.save.assert_not_called()

    def test_check_adopts_successful_legacy_read(self):
        api = MagicMock()
        api.load.return_value = (None, None)
        api.legacy_success.return_value = 'https://example.test/old-success'
        with patch.dict(os.environ, {'CHECK_ONLY': 'false', 'READ_NUM': '120',
                                    'READ_DAY': '2026-11-16', 'GITHUB_EVENT_NAME': 'workflow_dispatch'}), \
             patch.object(daily, 'now', return_value=datetime(2026, 11, 16, 2, tzinfo=daily.TZ)), \
             patch.object(daily, 'GitHub', return_value=api), \
             patch('sys.argv', ['daily_read.py', 'check']):
            daily.main()
        self.assertEqual(api.save.call_args.args[0]['completed'], 120)


if __name__ == '__main__':
    unittest.main()
