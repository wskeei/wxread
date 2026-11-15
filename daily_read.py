"""Daily gate and resumable runner. Persistent state contains counts, never cookies."""
import argparse
import base64
from datetime import datetime, time as wall_time, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
STATE_BRANCH = 'wxread-state'
START_TIME = wall_time(1, 7)


def now():
    return datetime.now(TZ)


class GitHub:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.token = os.environ['GH_TOKEN']

    def api(self, path, method='GET', payload=None):
        url = f'https://api.github.com/repos/{self.repo}/{path}'
        for attempt in range(4):
            request = urllib.request.Request(
                url, method=method,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={'Authorization': f'Bearer {self.token}',
                         'Accept': 'application/vnd.github+json',
                         'Content-Type': 'application/json'},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
            except urllib.error.URLError:
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt)

    def load(self, day):
        path = f'contents/days/{day}.json?ref={STATE_BRANCH}'
        try:
            item = self.api(path)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, None
            raise
        state = json.loads(base64.b64decode(item['content']))
        if (state.get('date') != day or type(state.get('completed')) is not int
                or state['completed'] < 0):
            raise ValueError('Invalid durable daily state; refusing to risk a duplicate read.')
        return state, item['sha']

    def save(self, state):
        # Creating the branch twice is safe; a concurrent/previous creation returns 422.
        try:
            self.api(f'git/ref/heads/{STATE_BRANCH}')
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            try:
                self.api('git/refs', 'POST', {
                    'ref': f'refs/heads/{STATE_BRANCH}', 'sha': os.environ['GITHUB_SHA'],
                })
            except urllib.error.HTTPError as create_exc:
                if create_exc.code != 422:
                    raise
                self.api(f'git/ref/heads/{STATE_BRANCH}')
        for attempt in range(3):
            previous, sha = self.load(state['date'])
            # Do not replace a newer checkpoint with stale progress.
            if previous and previous['completed'] > state['completed']:
                raise RuntimeError('Remote reading progress is newer; refusing to overwrite it.')
            payload = {
                'message': f"chore: reading progress {state['date']} ({state['completed']})",
                'branch': STATE_BRANCH,
                'content': base64.b64encode(
                    (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode()
                ).decode(),
            }
            if sha:
                payload['sha'] = sha
            try:
                self.api(f"contents/days/{state['date']}.json", 'PUT', payload)
                return
            except urllib.error.HTTPError as exc:
                # A successful PUT whose response was lost can also lead to a conflict.
                if exc.code not in (409, 422) or attempt == 2:
                    raise

    def legacy_success(self, day):
        """Adopt today's old workflow once when upgrading, avoiding a second full read."""
        start = datetime.combine(day, wall_time(), TZ)
        end = start + timedelta(days=1)
        query = urllib.parse.urlencode({
            'branch': os.environ.get('GITHUB_REF_NAME', 'main'),
            'created': f'{start.astimezone(ZoneInfo("UTC")).isoformat()}..{end.astimezone(ZoneInfo("UTC")).isoformat()}',
            'per_page': 100,
        })
        runs = self.api(f'actions/workflows/deploy.yml/runs?{query}')['workflow_runs']
        for run in runs:
            if str(run['id']) == os.environ.get('GITHUB_RUN_ID'):
                continue
            jobs = self.api(f"actions/runs/{run['id']}/jobs?per_page=100")['jobs']
            for job in jobs:
                for step in job.get('steps', []):
                    if step['name'] == 'Run deployment script' and step['conclusion'] == 'success':
                        finished = datetime.fromisoformat(step['completed_at'].replace('Z', '+00:00'))
                        if finished.astimezone(TZ).date() == day:
                            return run['html_url']
        return None


def decision(current, state, target, scheduled):
    if scheduled and current.time() < START_TIME:
        return False, '尚未到北京时间 01:07'
    if state and state['completed'] >= target:
        return False, f'当天已完成 {state["completed"]} 次阅读'
    return True, '当天尚未完成，开始阅读或补跑'


def checkpoint(path, fallback):
    if not path.exists():
        return fallback
    value = json.loads(path.read_text())['completed']
    if not isinstance(value, int) or value < fallback:
        raise ValueError('Invalid local reading checkpoint')
    return value


def run_daily(api, state, target):
    day = state['date']
    path = Path(os.environ.get('RUNNER_TEMP', '/tmp')) / f'wxread-{os.environ["GITHUB_RUN_ID"]}.json'
    # A rerun of the same Actions run must start from the durable checkpoint.
    path.unlink(missing_ok=True)
    deadline = min(now() + timedelta(hours=3, minutes=20),
                   datetime.combine(datetime.fromisoformat(day).date() + timedelta(days=1), wall_time(), TZ)
                   - timedelta(seconds=45))
    # Verify durable storage before sending reading requests.
    state.update(updated_at=now().isoformat(), run_id=os.environ['GITHUB_RUN_ID'])
    api.save(state)
    last_saved = state['completed']
    for attempt in range(1, 4):
        if now() >= deadline or now().date().isoformat() != day:
            break
        env = dict(os.environ, READ_NUM=str(target), READ_START_INDEX=str(state['completed']),
                   READ_PROGRESS_FILE=str(path), WXREAD_TARGET_DATE=day, PYTHONUNBUFFERED='1')
        print(f"Attempt {attempt}/3: {state['completed']}/{target}", flush=True)
        process = subprocess.Popen([sys.executable, 'main.py'], env=env)
        next_save = time.monotonic() + 300
        try:
            while process.poll() is None:
                try:
                    process.wait(timeout=min(30, max(0.1, (deadline - now()).total_seconds())))
                except subprocess.TimeoutExpired:
                    pass
                if now() >= deadline and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                state['completed'] = checkpoint(path, state['completed'])
                if time.monotonic() >= next_save and state['completed'] != last_saved:
                    state.update(updated_at=now().isoformat(), run_id=os.environ['GITHUB_RUN_ID'])
                    api.save(state)
                    last_saved = state['completed']
                    next_save = time.monotonic() + 300
        finally:
            # API/checkpoint failures must not leave a child continuing to read untracked.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        state['completed'] = checkpoint(path, state['completed'])
        state.update(updated_at=now().isoformat(), run_id=os.environ['GITHUB_RUN_ID'])
        api.save(state)
        if state['completed'] >= target:
            print(f'Daily reading completed: {day}, {state["completed"]}/{target}', flush=True)
            return
        if attempt < 3 and (deadline - now()).total_seconds() > 120:
            time.sleep(60)
    raise RuntimeError(f'Daily reading incomplete: {state["completed"]}/{target}; next scheduled check will resume.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['check', 'run'])
    args = parser.parse_args()
    target = int(os.environ.get('READ_NUM') or 40)
    if not 1 <= target <= 360:
        raise ValueError('READ_NUM must be between 1 and 360 (up to 180 minutes/day).')
    current = now()
    day = os.environ.get('READ_DAY') or current.date().isoformat()
    if day != current.date().isoformat():
        raise RuntimeError('The queued job belongs to a different day; wait for the next daily check.')
    api = GitHub()
    state, _ = api.load(day)
    state = state or {'date': day, 'completed': 0}
    should_run, reason = decision(current, state, target, os.environ.get('GITHUB_EVENT_NAME') == 'schedule')
    # Only needed before the new state system has its first checkpoint.
    if should_run and not state['completed']:
        legacy = api.legacy_success(current.date())
        if legacy:
            should_run, reason = False, f'当天旧版任务已完成：{legacy}'
            if os.environ.get('CHECK_ONLY') != 'true':
                state.update(completed=target, updated_at=current.isoformat(), legacy_run=legacy)
                api.save(state)
    print(f'{day}: {reason}', flush=True)
    if args.command == 'check':
        output = os.environ.get('GITHUB_OUTPUT')
        if output:
            with open(output, 'a') as file:
                file.write(f'should_run={str(should_run).lower()}\nday={day}\nkeepalive={str(current.hour == 1).lower()}\n')
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a') as file:
                file.write(f'北京时间日期：{day}\n\n{reason}。目标：{target} 次。\n')
    elif should_run:
        run_daily(api, state, target)


if __name__ == '__main__':
    main()
