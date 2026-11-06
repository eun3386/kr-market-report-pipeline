"""아침/오후 작업을 동일 OS 파일 잠금으로 직렬화한다 (충돌 시 실패, 강제 탈취 안 함)."""
import fcntl
import pathlib
import subprocess
import sys


def main():
    root = pathlib.Path(__file__).resolve().parent
    if len(sys.argv) != 2 or sys.argv[1] not in ('run_all.sh', 'run_premarket.sh', 'run_closing_check.sh'):
        raise SystemExit('Usage: pipeline_lock.py run_all.sh|run_premarket.sh|run_closing_check.sh')
    with (root / '.publication.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('다른 공개 발행 작업 실행 중 — 중복 실행 거부', file=sys.stderr)
            return 1
        # wrapper가 끝날 때까지 부모가 fd를 보유. 비정상 종료 시에도 OS가 잠금을 회수한다.
        return subprocess.call(['/bin/bash', str(root / sys.argv[1]), '--publication-locked'],
                               pass_fds=(lock.fileno(),))


if __name__ == '__main__':
    sys.exit(main())
