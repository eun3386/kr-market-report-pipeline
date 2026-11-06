#!/bin/bash

export TZ=Asia/Seoul
if [ "${1:-}" != "--publication-locked" ]; then
    exec /usr/bin/python3 "$(dirname "$0")/pipeline_lock.py" run_premarket.sh
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# 다른 feature 브랜치를 체크아웃해둔 채로 자동 실행돼 리포트/발행 커밋이 엉뚱한
# 브랜치에 쌓인 사고가 있었다(2026-09-07). master가 아니면 아무것도 하지 않고 중단한다.
REPO_BRANCH=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
if [ "$REPO_BRANCH" != "master" ]; then
    echo "$(date) 실행 중단: 현재 브랜치가 master가 아니라 '$REPO_BRANCH' — 자동 실행 안전장치" >> "$REPO/finance_data/run_log.txt"
    osascript -e "display notification \"master가 아니라 '$REPO_BRANCH' 브랜치라 자동 실행을 건너뜁니다\" with title \"⚠️ 브랜치 확인 필요\" sound name \"Basso\"" >> "$REPO/finance_data/run_log.txt" 2>&1
    exit 1
fi

# 덮개를 닫아서 절전(clamshell sleep)에 들어가면 Claude API 등 오래 걸리는 요청의
# 연결이 중간에 끊길 수 있어서, 이 스크립트가 끝날 때까지 절전을 막아둔다.
caffeinate -i -m -w $$ &

cd "$REPO/finance_data"
source venv/bin/activate || exit 1
# 글로벌 전용 모드는 KRX를 임포트하지 않아 자격증명도 필요 없다.

# GitHub 웹 등에서 portfolio_config.md/템플릿을 직접 수정했을 수 있으니 먼저 최신 상태로 동기화.
cd "$REPO"
attempt=1
max_attempts=3
while [ $attempt -le $max_attempts ]; do
    git pull --no-edit >> "$REPO/finance_data/run_log.txt" 2>&1
    PULL_EXIT=$?
    echo "git pull(시작 전 동기화) 종료 코드: $PULL_EXIT (시도 $attempt/$max_attempts)" >> "$REPO/finance_data/run_log.txt"
    if [ $PULL_EXIT -eq 0 ]; then
        break
    fi
    if [ $attempt -lt $max_attempts ]; then
        sleep 30
    fi
    attempt=$((attempt + 1))
done
[ "$PULL_EXIT" -eq 0 ] || exit "$PULL_EXIT"
cd "$REPO/finance_data"

# 간밤 미국/글로벌 지수만 수집 — KR 데이터는 16:00 run_all.sh가 담당한다.
python3 fetch_data.py --global-only >> run_log.txt 2>&1
GLOBAL_FETCH_EXIT=$?
echo "fetch_data.py --global-only 종료 코드: $GLOBAL_FETCH_EXIT" >> run_log.txt
[ "$GLOBAL_FETCH_EXIT" -eq 0 ] || exit "$GLOBAL_FETCH_EXIT"

# 개장 전 글로벌 브리핑 작성 (Read/Write/Glob만 허용, 무인 실행)
attempt=1
max_attempts=2
while [ $attempt -le $max_attempts ]; do
    CLAUDE_OUTPUT=$(/opt/homebrew/bin/claude -p "$(cat premarket_report_writer_instructions.md)" \
        --add-dir "$REPO" \
        --allowedTools "Read,Write,Glob" \
        --permission-mode bypassPermissions 2>&1)
    CLAUDE_EXIT=$?
    echo "$CLAUDE_OUTPUT" >> run_log.txt
    echo "프리마켓 리포트 작성(claude) 종료 코드: $CLAUDE_EXIT (시도 $attempt/$max_attempts)" >> run_log.txt
    if echo "$CLAUDE_OUTPUT" | grep -q "리포트 저장 완료"; then
        CLAUDE_EXIT=0
        break
    fi
    echo "경고: '리포트 저장 완료' 문구가 출력에 없음 — 실제로 리포트가 저장되지 않았을 수 있음" >> run_log.txt
    CLAUDE_EXIT=1
    if [ $attempt -lt $max_attempts ]; then
        sleep 30
    fi
    attempt=$((attempt + 1))
done

# 생성된 리포트를 git에 커밋 + 푸시
cd "$REPO"
GIT_EXIT=0
REPORT_PATH="finance_data/reports/$(date +%Y-%m-%d)_premarket.md"
if [ "$CLAUDE_EXIT" -eq 0 ] && [ -s "$REPORT_PATH" ]; then
    # 전 거래일에 산 추세 돌파 종목의 '오늘 대응표'를 리포트 맨 위에 붙인다. 전 거래일 확정 수급을 시장 전체로 4회만 조회하므로
    # 이 단계에서만 KRX 로그인 정보를 불러온다.
    source ~/.krx_env > /dev/null 2>&1
    python3 finance_data/breakout_signal.py insert "$REPORT_PATH" morning >> "$REPO/finance_data/run_log.txt" 2>&1
    echo "추세 돌파 섹션 추가 종료 코드: $?" >> "$REPO/finance_data/run_log.txt"
fi
if [ "$CLAUDE_EXIT" -eq 0 ] && [ -s "$REPORT_PATH" ] && [ -n "$(git status --porcelain -- "$REPORT_PATH")" ]; then
    # 기존 private origin/push는 변경하지 않고, 오늘 보고서만 커밋한다.
    git add -- "$REPORT_PATH"
    git commit --only -m "자동 프리마켓 리포트: $(date +%Y-%m-%d)" -- "$REPORT_PATH" >> "$REPO/finance_data/run_log.txt" 2>&1
    git pull --no-edit >> "$REPO/finance_data/run_log.txt" 2>&1
    git push >> "$REPO/finance_data/run_log.txt" 2>&1
    GIT_EXIT=$?
    echo "git 자동 커밋/푸시 종료 코드: $GIT_EXIT" >> "$REPO/finance_data/run_log.txt"
else
    echo "git 변경사항 없음 — 커밋 생략" >> "$REPO/finance_data/run_log.txt"
fi

# 완성된 리포트를 iCloud로 동기화되는 Obsidian 볼트에 복사해서 아이폰 Obsidian
# 앱에서 바로 열리게 한다. cwd가 아직 저장소 루트라 REPORT_PATH가 그대로 유효하다 —
# finance_data로 cd한 뒤에 이 블록을 두면 경로가 어긋나므로 순서를 지킨다.
OBSIDIAN_VAULT_DIR="${OBSIDIAN_VAULT_DIR:-}"  # 선택: 리포트를 복사할 Obsidian 볼트 경로
if [ "$CLAUDE_EXIT" -eq 0 ] && [ -s "$REPORT_PATH" ] && [ -d "$OBSIDIAN_VAULT_DIR" ]; then
    cp "$REPORT_PATH" "$OBSIDIAN_VAULT_DIR/" >> "$REPO/finance_data/run_log.txt" 2>&1
    echo "Obsidian 볼트 복사 종료 코드: $?" >> "$REPO/finance_data/run_log.txt"
fi

# 디스코드는 파일 첨부 없이 "리포트 도착" 알림만 보낸다 — 실제 내용은 Obsidian에서 본다.
# ~/.discord_webhook_env에 DISCORD_WEBHOOK_URL이 없으면 조용히 건너뛴다.
if [ "$CLAUDE_EXIT" -eq 0 ] && [ -s "$REPORT_PATH" ]; then
    source ~/.discord_webhook_env > /dev/null 2>&1
    if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
        DISCORD_HTTP=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
            -H "Content-Type: application/json" \
            -d "{\"content\":\"🌅 프리마켓 브리핑 도착: $(date +%Y-%m-%d) — Obsidian에서 확인하세요\"}" \
            "$DISCORD_WEBHOOK_URL")
        echo "디스코드 알림 HTTP $DISCORD_HTTP" >> "$REPO/finance_data/run_log.txt"
    fi
fi
cd "$REPO/finance_data"

# 공개 미니앱(프리마켓 인사이트)의 글로벌 스냅샷 발행은 GitHub Actions
# (premarket-insight-app .github/workflows/sync-stock-prices.yml, 07:20 KST global cron)가
# 전담한다. 이 Mac은 더 이상 운영 Supabase에 공개 데이터를 쓰지 않는다 — 켜져 있어야 한다는
# 의존성을 없애기 위함 (2026-09-07, 같은 스케줄에서 Mac과 GitHub Actions가 이중 발행하던 문제 정리).

if [ $PULL_EXIT -eq 0 ] && [ $GLOBAL_FETCH_EXIT -eq 0 ] && [ $CLAUDE_EXIT -eq 0 ] && [ $GIT_EXIT -eq 0 ]; then
    PIPELINE_EXIT=0
    osascript -e 'display notification "글로벌 데이터 수집 및 프리마켓 브리핑 완료" with title "✅ 프리마켓 브리핑 완료" sound name "Glass"' >> run_log.txt 2>&1
else
    PIPELINE_EXIT=1
    osascript -e "display notification \"pull:$PULL_EXIT global_fetch:$GLOBAL_FETCH_EXIT claude:$CLAUDE_EXIT git:$GIT_EXIT\" with title \"⚠️ 프리마켓 브리핑 실패\" sound name \"Basso\"" >> run_log.txt 2>&1
fi

echo "=== $(date) 프리마켓 실행 완료 ===" >> run_log.txt
exit "$PIPELINE_EXIT"
