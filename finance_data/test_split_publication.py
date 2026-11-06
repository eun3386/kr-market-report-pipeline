"""로컬 단위 테스트. KRX 임포트/운영 Supabase 요청/파이프라인 실행 없음."""
import ast
import datetime
import io
import json
import os
import pathlib
import re
import unittest
import tempfile
import fcntl
import subprocess
from unittest.mock import patch, Mock
from contextlib import ExitStack, redirect_stdout
import snapshot_validation as validation
import publish_public_snapshot as publisher
import pipeline_lock

ROOT = pathlib.Path(__file__).resolve().parent


class SplitPublicationTest(unittest.TestCase):
    def setUp(self):
        self.today = datetime.datetime(2026, 9, 7, 16, tzinfo=validation.KST)
        self.cache = {'date': '2026-09-07', 'fetched_at': '2026-09-06T22:20:00Z',
                      'global': {'SP': {'close': 100, 'pct_change': 1}}}
        self.data = {
            'flow': {**{m: {'외국인': 1, '기관합계': 2, '개인': -3} for m in ('코스피', '코스닥')},
                     '코스피_프로그램매매': {'날짜': '2026-09-07'}},
            'watchlist_candidates': [{'code': '000660', 'name': 'fixture', 'close': 100}],
            'technical_rebound_candidates': [{'code': '005930', 'name': 'fixture', 'close': 200}],
            'global': self.cache['global'], 'global_snapshot_date': self.cache['date'],
            'global_fetched_at': self.cache['fetched_at'],
        }

    def publisher_context(self, *, enabled=True):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(validation, 'kst_now', return_value=self.today))
        stack.enter_context(patch.object(publisher, 'kst_now', return_value=self.today))
        stack.enter_context(patch.object(publisher, '_load_json', side_effect=lambda name: {
            'global_data.json': self.cache, 'market_data.json': self.data, 'scraped_data.json': {},
        }[name]))
        stack.enter_context(patch.dict(publisher.os.environ, {
            'SUPABASE_URL': 'https://mock.invalid/', 'SUPABASE_SERVICE_ROLE_KEY': 'mock-secret',
            'PUBLIC_SNAPSHOT_PUBLISH_ENABLED': '1' if enabled else '',
        }, clear=True))
        post = stack.enter_context(patch.object(publisher.requests, 'post', return_value=Mock(status_code=204)))
        delete = stack.enter_context(patch.object(publisher.requests, 'delete', side_effect=AssertionError('No REST delete allowed')))
        return post, delete

    def test_default_and_explicit_dry_run_never_send_even_when_gate_enabled(self):
        post, delete = self.publisher_context()
        for args in ([], ['--dry-run'], ['--global-only'], ['--global-only', '--dry-run']):
            self.assertEqual(publisher.main(args), 0)
        post.assert_not_called()
        delete.assert_not_called()

    def test_dry_run_needs_no_credentials(self):
        post, _ = self.publisher_context()
        with patch.dict(publisher.os.environ, {}, clear=True):
            self.assertEqual(publisher.main([]), 0)
            self.assertEqual(publisher.main(['--global-only']), 0)
        post.assert_not_called()

    def test_explicit_publish_requires_gate(self):
        post, _ = self.publisher_context(enabled=False)
        for args in (['--publish'], ['--global-only', '--publish']):
            self.assertEqual(publisher.main(args), 1)
        post.assert_not_called()

    def test_publish_requires_https_and_key(self):
        post, _ = self.publisher_context()
        for env in ({'SUPABASE_URL': 'http://mock.invalid'}, {'SUPABASE_SERVICE_ROLE_KEY': ''}):
            with patch.dict(publisher.os.environ, env):
                self.assertEqual(publisher.main(['--publish']), 1)
        post.assert_not_called()

    def test_global_uses_only_separate_table(self):
        post, delete = self.publisher_context()
        self.assertEqual(publisher.main(['--global-only', '--publish']), 0)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://mock.invalid/rest/v1/public_global_snapshots?on_conflict=snapshot_date')
        self.assertEqual(set(kwargs['json']), {'snapshot_date', 'global_indicators', 'global_indicators_published_at'})
        self.assertEqual(kwargs['json']['global_indicators_published_at'], self.cache['fetched_at'])
        self.assertFalse(kwargs['allow_redirects'])
        delete.assert_not_called()

    def test_kr_single_rpc_carries_children_and_fallback(self):
        post, delete = self.publisher_context()
        self.assertEqual(publisher.main(['--publish']), 0)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], 'https://mock.invalid/rest/v1/rpc/publish_kr_snapshot')
        payload = post.call_args.kwargs['json']
        self.assertEqual(set(payload), {'p_snapshot', 'p_watchlist', 'p_rebound', 'p_global_fallback'})
        self.assertEqual(payload['p_watchlist'][0]['stock_code'], '000660')
        self.assertEqual(payload['p_rebound'][0]['stock_code'], '005930')
        self.assertEqual(payload['p_global_fallback'], publisher._global_row(self.cache))
        self.assertNotIn('global_indicators', payload['p_snapshot'])
        for row in [payload['p_snapshot'], *payload['p_watchlist'], *payload['p_rebound']]:
            self.assertEqual(row['snapshot_date'], '2026-09-07')
        delete.assert_not_called()

    def test_bad_fallback_does_not_block_valid_kr_or_overwrite_global(self):
        post, _ = self.publisher_context()
        for key, value in [('global_snapshot_date', '2026-09-04'), ('global', {'SP': '확인불가'}),
                           ('global_fetched_at', 'invalid')]:
            with patch.dict(self.data, {key: value}):
                self.assertEqual(publisher.main(['--publish']), 0)
                self.assertIsNone(post.call_args.kwargs['json']['p_global_fallback'])

    def test_empty_candidates_are_explicit_empty_arrays(self):
        post, _ = self.publisher_context()
        self.data['watchlist_candidates'] = []
        self.data['technical_rebound_candidates'] = []
        self.assertEqual(publisher.main(['--publish']), 0)
        self.assertEqual(post.call_args.kwargs['json']['p_watchlist'], [])
        self.assertEqual(post.call_args.kwargs['json']['p_rebound'], [])

    def test_http_and_transport_failure_never_fall_back_to_rest_or_leak(self):
        post, delete = self.publisher_context()
        output = io.StringIO()
        with redirect_stdout(output):
            for status in (302, 401, 404, 500):
                post.return_value.status_code = status
                self.assertEqual(publisher.main(['--publish']), 1)
            post.side_effect = publisher.requests.Timeout('mock-secret sensitive response')
            self.assertEqual(publisher.main(['--publish']), 1)
        self.assertNotIn('mock-secret', output.getvalue())
        self.assertNotIn('발행 완료', output.getvalue())
        self.assertEqual(post.call_count, 5)
        delete.assert_not_called()

    def test_invalid_json_and_non_object_rejected_without_send(self):
        post, _ = self.publisher_context()
        with patch.object(publisher, '_load_json', side_effect=json.JSONDecodeError('invalid', '', 0)):
            self.assertEqual(publisher.main(['--publish']), 1)
            self.assertEqual(publisher.main(['--global-only', '--publish']), 1)
        with patch.object(publisher, '_load_json', return_value=[]):
            self.assertEqual(publisher.main(['--publish']), 1)
        post.assert_not_called()

    def test_conflicting_cli_modes_rejected(self):
        post, _ = self.publisher_context()
        with redirect_stdout(io.StringIO()), patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                publisher.main(['--dry-run', '--publish'])
        self.assertEqual(error.exception.code, 2)
        post.assert_not_called()

    def test_config_reader_matches_portfolio_config_fixed_watchlist(self):
        # 설정 파일의 "관심종목 (고정)" 항목 수를 직접 세어 비교한다 —
        # 종목 수를 하드코딩하면 종목을 추가/삭제할 때마다 깨진다.
        tree = ast.parse((ROOT / 'fetch_data.py').read_text())
        nodes = [n for n in tree.body if
                 (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ('_REPO_ROOT', 'TEMPLATE_PATH') for t in n.targets))
                 or (isinstance(n, ast.If) and 'TEMPLATE_PATH' in ast.unparse(n.test))
                 or (isinstance(n, ast.FunctionDef) and n.name == '_load_fixed_watchlist_names')]
        ns = {'re': re, 'os': os, '__file__': str(ROOT / 'fetch_data.py')}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<isolated-reader>', 'exec'), ns)
        section = re.search(r'### 관심종목 \(고정\)\n\n(.*?)\n\n###',
                            pathlib.Path(ns['TEMPLATE_PATH']).read_text(), re.S)
        expected = len(re.findall(r'^- ', section.group(1), re.M))
        self.assertGreater(expected, 0)
        self.assertEqual(len(ns['_load_fixed_watchlist_names']()), expected)

    def test_cache_dates_and_failures(self):
        self.assertTrue(validation.valid_cache(self.cache, '2026-09-07'))
        self.assertFalse(validation.valid_cache(self.cache, '2026-09-04'))
        self.assertFalse(validation.valid_cache(None, '2026-09-07'))
        self.cache['global']['SP'] = '확인불가'
        self.assertFalse(validation.valid_cache(self.cache, '2026-09-07'))
        self.assertFalse(validation.has_global_values(self.cache['global']))

    def test_cache_requires_timezone_and_matching_kst_day(self):
        for timestamp in ('2026-09-07T07:20:00', 'invalid', '2026-09-05T22:20:00Z'):
            self.cache['fetched_at'] = timestamp
            self.assertFalse(validation.valid_cache(self.cache, '2026-09-07'))

    def test_invalid_kr_rejected_before_network(self):
        for data in ({}, {'flow': {'코스피_프로그램매매': {'날짜': '2026-09-04'}}}):
            with patch.object(validation, 'kst_now', return_value=self.today), \
                 patch.object(publisher, '_load_json', return_value=data), \
                 patch.object(publisher.requests, 'post') as post, \
                 patch.dict(publisher.os.environ, {'SUPABASE_URL': 'https://mock.invalid', 'SUPABASE_SERVICE_ROLE_KEY': 'mock'}):
                self.assertEqual(publisher.publish(), 1)
                post.assert_not_called()

    def test_valid_kr_and_missing_market(self):
        market = {k: {'외국인': 1, '기관합계': 2, '개인': -3} for k in ('코스피', '코스닥')}
        data = {'flow': {**market, '코스피_프로그램매매': {'날짜': '2026-09-07'}},
                'watchlist_candidates': [], 'technical_rebound_candidates': []}
        with patch.object(validation, 'kst_now', return_value=self.today):
            self.assertEqual(validation.validate_kr(data), '2026-09-07')
            data['flow']['코스닥'] = '조회 실패'
            with self.assertRaises(ValueError): validation.validate_kr(data)

    def test_invalid_global_not_published(self):
        with patch.object(publisher, 'kst_now', return_value=self.today), \
             patch.object(publisher, '_load_json', return_value={**self.cache, 'date': '2026-09-04'}), \
             patch.object(publisher.requests, 'post') as post, \
             patch.dict(publisher.os.environ, {'SUPABASE_URL': 'https://mock.invalid', 'SUPABASE_SERVICE_ROLE_KEY': 'mock'}):
            self.assertEqual(publisher.publish_global_only(), 1)
            post.assert_not_called()

    def test_loader_retries_partial_error_cache(self):
        tree = ast.parse((ROOT / 'fetch_data.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_load_global_cache')
        ns = {'json': json, 'GLOBAL_DATA_CACHE_PATH': 'unused', 'valid_cache': validation.valid_cache}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<isolated-cache>', 'exec'), ns)
        self.cache['global']['VIX'] = '확인불가'
        ns['open'] = lambda *a, **k: io.StringIO(json.dumps(self.cache))
        self.assertIsNone(ns['_load_global_cache']('2026-09-07'))

    def test_shared_lock_rejects_overlap_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            def child(*args, **kwargs):
                self.assertEqual(len(kwargs['pass_fds']), 1)
                with open(pathlib.Path(folder) / '.publication.lock', 'a') as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return 0
            with patch.object(pipeline_lock, '__file__', str(pathlib.Path(folder) / 'pipeline_lock.py')), \
                 patch.object(pipeline_lock.sys, 'argv', ['pipeline_lock.py', 'run_all.sh']), \
                 patch.object(pipeline_lock.subprocess, 'call', side_effect=child):
                self.assertEqual(pipeline_lock.main(), 0)
                self.assertEqual(pipeline_lock.main(), 0)

    def test_wrappers_refuse_to_run_off_master(self):
        # 2026-09-07: feature 브랜치를 체크아웃해둔 채로 자동 실행돼 리포트/발행
        # 커밋이 master가 아닌 곳에 쌓인 사고가 있었다 — 재발 방지 가드.
        for filename in ('run_all.sh', 'run_premarket.sh'):
            with self.subTest(filename=filename):
                script = (ROOT / filename).read_text()
                guard_pos = script.index("rev-parse --abbrev-ref HEAD")
                caffeinate_pos = script.index('caffeinate')
                self.assertLess(guard_pos, caffeinate_pos)
                self.assertIn('!= "master" ]', script)
                self.assertIn('exit 1', script[guard_pos:caffeinate_pos])

    def test_auto_commit_is_limited_to_daily_report(self):
        for filename, kind in [('run_all.sh', 'postmarket'), ('run_premarket.sh', 'premarket')]:
            script = (ROOT / filename).read_text()
            self.assertNotIn('git add -A', script)
            self.assertIn(f'_{kind}.md', script)
            self.assertIn('git add -- "$REPORT_PATH"', script)
            self.assertIn('git commit --only', script)
            self.assertIn('[ "$CLAUDE_EXIT" -eq 0 ]', script)

    def test_discord_notification_runs_before_cwd_changes_to_finance_data(self):
        # REPORT_PATH는 저장소 루트 기준 상대경로("finance_data/reports/...")다.
        # 디스코드 전송 블록이 "cd .../finance_data" 뒤로 밀리면 같은 변수가 더 이상
        # 유효한 경로를 가리키지 않아 [-s "$REPORT_PATH"]가 조용히 실패한다
        # (2026-09-09 실제로 이 순서 문제로 알림이 한 번도 전송되지 않은 채 넘어간 사고).
        for filename in ('run_all.sh', 'run_premarket.sh'):
            with self.subTest(filename=filename):
                script = (ROOT / filename).read_text()
                discord_pos = script.index('DISCORD_WEBHOOK_URL')
                cd_positions = [m.start() for m in re.finditer(
                    r'cd "\$REPO/finance_data"\n', script)]
                cd_after_git = [p for p in cd_positions if p > script.index('git commit --only')]
                self.assertTrue(cd_after_git, f'{filename}: git commit 이후 finance_data로 되돌아가는 cd가 없음')
                self.assertLess(discord_pos, cd_after_git[0],
                                 f'{filename}: 디스코드 전송이 finance_data로 cd한 뒤에 있음')

    def test_wrappers_do_not_publish_public_data(self):
        # 공개 데이터 발행(publish_public_snapshot.py)과 관심종목 가격 동기화
        # (sync_stock_prices.py)는 GitHub Actions가 전담한다. 2026-09-07에 이 Mac이
        # 두 스크립트를 다시 호출하도록 되돌려져 GitHub Actions와 같은 날짜를 이중
        # 발행한 사고가 있었다 — 재발 방지용 회귀 테스트.
        for filename in ('run_all.sh', 'run_premarket.sh'):
            with self.subTest(filename=filename):
                script = (ROOT / filename).read_text()
                self.assertNotIn('publish_public_snapshot.py', script)
                self.assertNotIn('sync_stock_prices.py', script)
                self.assertNotIn('PUBLIC_SNAPSHOT_PUBLISH_ENABLED', script)

    def run_wrapper_tail(self, filename, *, report_exit=0, git_exit=0):
        # 개인 리포트 작성 이후(공개 발행 없음) 코드만 격리 실행한다.
        # 수집/Claude/Git/잠금 단계는 실행하지 않고 osascript만 mock한다.
        script = (ROOT / filename).read_text()
        tail = script[script.index('\nif [ $PULL_EXIT'):]
        self.assertNotRegex(tail, r'(?m)^\s*(source|git|cd|caffeinate|exec|python3)\s')
        prelude = 'osascript() { echo "MOCK_NOTICE:$*"; }\n'
        env = {key: '0' for key in ('PULL_EXIT', 'FETCH_DATA_EXIT', 'FETCH_KR_EXIT',
               'FETCH_SCRAPED_EXIT', 'FETCH_PROGRAM_EXIT', 'GLOBAL_FETCH_EXIT')}
        env.update(PATH='/usr/bin:/bin', TZ='Asia/Seoul', CLAUDE_EXIT=str(report_exit),
                   GIT_EXIT=str(git_exit))
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(['/bin/bash', '-c', prelude + tail], cwd=folder, env=env,
                                    capture_output=True, text=True, timeout=5)
            log = (pathlib.Path(folder) / 'run_log.txt').read_text()
        self.assertEqual(result.stderr, '')
        return result.returncode, log

    def test_wrappers_success_notifies_glass(self):
        for filename in ('run_all.sh', 'run_premarket.sh'):
            with self.subTest(filename=filename):
                code, log = self.run_wrapper_tail(filename)
                self.assertEqual(code, 0)
                self.assertIn('✅', log)
                self.assertNotIn('⚠️', log)

    def test_wrappers_report_failure_propagates(self):
        for filename in ('run_all.sh', 'run_premarket.sh'):
            with self.subTest(filename=filename):
                code, log = self.run_wrapper_tail(filename, report_exit=1)
                self.assertEqual(code, 1)
                self.assertIn('claude:1', log)
                self.assertNotIn('✅', log)

    def test_wrappers_git_failure_propagates(self):
        for filename in ('run_all.sh', 'run_premarket.sh'):
            with self.subTest(filename=filename):
                code, log = self.run_wrapper_tail(filename, git_exit=1)
                self.assertEqual(code, 1)
                self.assertIn('git:1', log)
                self.assertNotIn('✅', log)


if __name__ == '__main__':
    unittest.main()
