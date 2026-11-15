"""Exercise the actual reading loop without network, credentials, or waiting."""
import json
import os
from pathlib import Path
import runpy
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


class ReadingProgressTests(unittest.TestCase):
    def execute(self, response, folder, start=118, target_date=None):
        config = types.ModuleType('config')
        config.data = {'s': 'initial'}
        config.headers, config.cookies = {}, {}
        config.READ_NUM, config.PUSH_METHOD = 120, ''
        config.book, config.chapter = ['book'], ['chapter']
        requests = types.ModuleType('requests')
        requests.RequestException = ConnectionError
        renewal = MagicMock(cookies={'wr_skey': 'abcdefgh'})
        read = MagicMock(status_code=200)
        read.json.return_value = response
        requests.post = MagicMock(side_effect=lambda url, **kwargs: renewal if 'renewal' in url else read)
        push = types.ModuleType('push')
        push.push = MagicMock()
        log = types.ModuleType('log_utils')
        log.setup_logging = MagicMock(return_value=MagicMock())
        env = {'READ_START_INDEX': str(start), 'READ_PROGRESS_FILE': str(Path(folder) / 'progress.json'),
               'WXREAD_TARGET_DATE': target_date or ''}
        with patch.dict('sys.modules', {'config': config, 'requests': requests, 'push': push, 'log_utils': log}), \
             patch.dict(os.environ, env), patch('time.sleep'):
            runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'), run_name='__main__')
        return requests

    def test_actual_loop_only_reads_remaining_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            requests = self.execute({'succ': 1, 'synckey': 100}, folder)
            self.assertEqual(json.loads((Path(folder) / 'progress.json').read_text())['completed'], 120)
            read_calls = [call for call in requests.post.call_args_list if '/book/read' in call.args[0]]
            self.assertEqual(len(read_calls), 2)

    def test_unsuccessful_response_never_advances_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, '连续 3 次'):
                self.execute({'succ': 0, 'synckey': 100, 'errCode': -2012}, folder)
            self.assertFalse((Path(folder) / 'progress.json').exists())

    def test_cross_day_stops_before_reading(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, '跨日'):
                self.execute({'succ': 1, 'synckey': 100}, folder, target_date='2000-01-01')
            self.assertFalse((Path(folder) / 'progress.json').exists())


if __name__ == '__main__':
    unittest.main()
