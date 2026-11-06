#!/bin/bash

export TZ=Asia/Seoul
if [ "${1:-}" != "--publication-locked" ]; then
    exec /usr/bin/python3 "$(dirname "$0")/pipeline_lock.py" run_closing_check.sh
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG=$REPO/finance_data/run_log.txt

REPO_BRANCH=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
if [ "$REPO_BRANCH" != "master" ]; then
    echo "$(date) 종가배팅 체크 중단: 현재 브랜치가 master가 아니라 '$REPO_BRANCH'" >> "$LOG"
    osascript -e "display notification \"master가 아니라 '$REPO_BRANCH' 브랜치라 종가배팅 체크를 건너뜁니다\" with title \"⚠️ 브랜치 확인 필요\" sound name \"Basso\"" >> "$LOG" 2>&1
    exit 1
fi

caffeinate -i -m -w $$ &

cd "$REPO/finance_data"
source venv/bin/activate || exit 1
source ~/.krx_env > /dev/null 2>&1
source ~/.discord_webhook_env > /dev/null 2>&1

# 절전에서 깨어난 직후 DNS가 늦게 붙어 15:10 실행이 통째로 실패한 일이 있어(2026-10-20), 최대 2분 기다린다.
for _ in $(seq 1 24); do
    /usr/bin/host -W 2 data.krx.co.kr > /dev/null 2>&1 && break
    sleep 5
done

# 15:20 동시호가 전에 결과를 받아야 하므로 git pull은 1회만 시도하고, 실패해도 기다리거나 중단하지 않는다.
git -C "$REPO" pull --no-edit >> "$LOG" 2>&1
echo "git pull(종가배팅 체크) 종료 코드: $?" >> "$LOG"

for attempt in 1 2; do
    CHECK_OUTPUT=$(python3 closing_check.py 2>> "$LOG")
    CHECK_EXIT=$?
    echo "$CHECK_OUTPUT" >> "$LOG"
    echo "closing_check.py 종료 코드: $CHECK_EXIT (시도 $attempt/2)" >> "$LOG"
    if [ "$CHECK_EXIT" -eq 0 ] || [ "$CHECK_EXIT" -eq 3 ]; then break; fi
    [ "$attempt" -eq 1 ] && sleep 20
done
# 휴장일/장 시작 전에는 조용히 종료한다.
if [ "$CHECK_EXIT" -eq 3 ]; then exit 0; fi

cd "$REPO"
TODAY=$(date +%Y-%m-%d)
REPORT_PATH="finance_data/reports/${TODAY}_closing_check.md"
SUMMARY=$(echo "$CHECK_OUTPUT" | grep '^요약:' | tail -1 | sed 's/^요약: //')
GIT_EXIT=0

if [ "$CHECK_EXIT" -eq 0 ] && [ -s "$REPORT_PATH" ]; then
    # 시간이 급한 알림(Obsidian 복사, 디스코드)을 먼저 보내고 git 커밋/푸시는 마지막에 한다.
    OBSIDIAN_VAULT_DIR="${OBSIDIAN_VAULT_DIR:-}"  # 선택: 리포트를 복사할 Obsidian 볼트 경로
    if [ -d "$OBSIDIAN_VAULT_DIR" ]; then
        cp "$REPORT_PATH" "$OBSIDIAN_VAULT_DIR/" >> "$LOG" 2>&1
        echo "Obsidian 볼트 복사 종료 코드: $?" >> "$LOG"
    fi

    if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
        DISCORD_HTTP=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
            -H "Content-Type: application/json" \
            -d "{\"content\":\"🔔 종가배팅 체크 (${TODAY} 잠정): ${SUMMARY} — Obsidian에서 확인하세요\"}" \
            "$DISCORD_WEBHOOK_URL")
        echo "디스코드 알림 HTTP $DISCORD_HTTP" >> "$LOG"
    fi

    if [ -n "$(git status --porcelain -- "$REPORT_PATH")" ]; then
        git add -- "$REPORT_PATH"
        git commit --only -m "자동 종가배팅 체크: $TODAY" -- "$REPORT_PATH" >> "$LOG" 2>&1
        git pull --no-edit >> "$LOG" 2>&1
        git push >> "$LOG" 2>&1
        GIT_EXIT=$?
        echo "git 자동 커밋/푸시 종료 코드: $GIT_EXIT" >> "$LOG"
    fi
fi

if [ "$CHECK_EXIT" -eq 0 ] && [ "$GIT_EXIT" -eq 0 ]; then
    PIPELINE_EXIT=0
    osascript -e "display notification \"${SUMMARY}\" with title \"🔔 종가배팅 체크 완료\" sound name \"Glass\"" >> "$LOG" 2>&1
else
    PIPELINE_EXIT=1
    osascript -e "display notification \"check:$CHECK_EXIT git:$GIT_EXIT\" with title \"⚠️ 종가배팅 체크 실패\" sound name \"Basso\"" >> "$LOG" 2>&1
    # 체크 자체가 실패하면 리포트가 아예 안 오므로 폰에서도 알 수 있게 디스코드로 알린다 (git만 실패한 경우는 리포트가 이미 발송됨).
    if [ "$CHECK_EXIT" -ne 0 ] && [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
        curl -s -o /dev/null -X POST -H "Content-Type: application/json" \
            -d "{\"content\":\"⚠️ 종가배팅 체크 실패 (${TODAY}, 종료 코드 ${CHECK_EXIT}) — 네트워크/데이터 조회 문제로 리포트가 생성되지 않았어요\"}" \
            "$DISCORD_WEBHOOK_URL" >> "$LOG" 2>&1
    fi
fi

echo "=== $(date) 종가배팅 체크 완료 ===" >> "$LOG"
exit "$PIPELINE_EXIT"
