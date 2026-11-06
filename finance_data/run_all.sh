#!/bin/bash

export TZ=Asia/Seoul
if [ "${1:-}" != "--publication-locked" ]; then
    exec /usr/bin/python3 "$(dirname "$0")/pipeline_lock.py" run_all.sh
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
# 연결이 중간에 끊길 수 있어서, 이 스크립트가 끝날 때까지 절전을 막아둔다
# (2026-08-20 리포트 작성 단계에서 "Connection closed mid-response"로 실패한 사고 확인).
caffeinate -i -m -w $$ &

cd "$REPO/finance_data"
source venv/bin/activate || exit 1
source ~/.krx_env > /dev/null 2>&1

# GitHub 웹 등에서 템플릿/포트폴리오를 직접 수정했을 수 있으니, 데이터 수집/리포트 작성 전에 먼저 최신 상태로 동기화.
# 절전에서 막 깨어난 직후라 네트워크가 아직 준비 안 된 상태일 수 있어(2026-08-21 DNS 조회
# 실패로 실제 발생 확인) 30초 간격으로 최대 3회 재시도.
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

# run_log.txt가 5MB를 넘으면 타임스탬프 파일로 회전하고, 회전된 로그는 최근 5개만 보관
MAX_LOG_SIZE=$((5 * 1024 * 1024))
if [ -f run_log.txt ] && [ "$(stat -f%z run_log.txt 2>/dev/null || stat -c%s run_log.txt)" -gt "$MAX_LOG_SIZE" ]; then
    mv run_log.txt "run_log_$(date +%Y%m%d_%H%M%S).txt"
    ls -t run_log_*.txt 2>/dev/null | tail -n +6 | xargs rm -f
fi

# fetch_data.py는 KRX 로그인 세션 문제로 가끔 실패할 수 있어 실패 시 60초 간격으로 최대 2회 재시도
# 장 마감 전/휴장일이라도 마지막 거래일 데이터로 강제 재생성한다(개인용 리포트라 날짜
# 가드가 없어도 됨). 기본값은 ON — 터미널에서 직접 `bash run_all.sh`로 수동 실행할 때
# 매번 FORCE_REGENERATE=1을 안 붙여도 되게 하려는 의도. launchd 예약 자동 실행은 plist의
# EnvironmentVariables에서 FORCE_REGENERATE=0을 명시적으로 줘서 이 기본값을 끈다
# (마감 전에는 이전 JSON을 재발행하지 않는 안전장치를 예약 실행에서는 유지해야 하므로).
FETCH_DATA_ARGS=()
if [ "${FORCE_REGENERATE:-1}" = "1" ]; then
    FETCH_DATA_ARGS+=(--force)
fi
attempt=1
max_attempts=3
while [ $attempt -le $max_attempts ]; do
    python3 fetch_data.py "${FETCH_DATA_ARGS[@]}" >> run_log.txt 2>&1
    FETCH_DATA_EXIT=$?
    echo "fetch_data.py 종료 코드: $FETCH_DATA_EXIT (시도 $attempt/$max_attempts)" >> run_log.txt
    # 휴장일/장 마감 전에는 이전 JSON을 재발행하지 않는다.
    if [ "$FETCH_DATA_EXIT" -eq 3 ]; then exit 0; fi
    if [ $FETCH_DATA_EXIT -eq 0 ]; then
        break
    fi
    if [ $attempt -lt $max_attempts ]; then
        sleep 60
    fi
    attempt=$((attempt + 1))
done
[ "$FETCH_DATA_EXIT" -eq 0 ] || exit "$FETCH_DATA_EXIT"

python3 fetch_kr_stocks.py >> run_log.txt 2>&1
FETCH_KR_EXIT=$?
echo "fetch_kr_stocks.py 종료 코드: $FETCH_KR_EXIT" >> run_log.txt
[ "$FETCH_KR_EXIT" -eq 0 ] || exit "$FETCH_KR_EXIT"

python3 fetch_scraped.py >> run_log.txt 2>&1
FETCH_SCRAPED_EXIT=$?
echo "fetch_scraped.py 종료 코드: $FETCH_SCRAPED_EXIT" >> run_log.txt
[ "$FETCH_SCRAPED_EXIT" -eq 0 ] || exit "$FETCH_SCRAPED_EXIT"

python3 fetch_program.py >> run_log.txt 2>&1
FETCH_PROGRAM_EXIT=$?
echo "fetch_program.py 종료 코드: $FETCH_PROGRAM_EXIT" >> run_log.txt
[ "$FETCH_PROGRAM_EXIT" -eq 0 ] || exit "$FETCH_PROGRAM_EXIT"

# market_data.json/program_data.json을 읽어 postmarket_report_template.md 형식대로
# 마감 후 스크리닝 리포트를 작성 (reports/<기준일>_postmarket.md). 무인 실행이라 승인 프롬프트 없이(bypassPermissions)
# 진행하되, 필요한 도구만 허용(Read/Write/Glob)한다.
# API 연결이 중간에 끊기는 일회성 오류가 실제로 발생한 적 있어(2026-08-20) 재시도 로직 추가.
attempt=1
max_attempts=2
while [ $attempt -le $max_attempts ]; do
    CLAUDE_OUTPUT=$(/opt/homebrew/bin/claude -p "$(cat postmarket_report_writer_instructions.md)" \
        --add-dir "$REPO" \
        --allowedTools "Read,Write,Glob" \
        --permission-mode bypassPermissions 2>&1)
    CLAUDE_EXIT=$?
    echo "$CLAUDE_OUTPUT" >> run_log.txt
    echo "리포트 작성(claude) 종료 코드: $CLAUDE_EXIT (시도 $attempt/$max_attempts)" >> run_log.txt
    # 프로세스가 정상 종료(0)해도 실제로 리포트를 저장하지 않고 질문/코멘트만 남겼을 수 있으니 확인
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

# 생성된 리포트 등 변경사항을 자동으로 git에 커밋 + 푸시 (private 저장소)
# FORCE_REGENERATE=1로 과거 거래일을 재생성했을 때는 리포트 파일명이 오늘 날짜가
# 아닐 수 있어, claude 출력에서 실제 저장 경로를 그대로 뽑아 쓴다.
cd "$REPO"
GIT_EXIT=0
REPORT_PATH=$(echo "$CLAUDE_OUTPUT" | grep -o 'reports/[0-9-]*_postmarket\.md' | tail -1)
REPORT_PATH="finance_data/${REPORT_PATH:-$(date +%Y-%m-%d)_postmarket.md}"
REPORT_DATE=$(basename "$REPORT_PATH" _postmarket.md)
# 주 신호(추세 돌파) 섹션은 리포트 작성 AI를 거치지 않고 Python이 계산해 리포트 맨 위(제목 바로 다음)에 붙인다.
if [ "$CLAUDE_EXIT" -eq 0 ] && [ -s "$REPORT_PATH" ]; then
    python3 finance_data/breakout_signal.py insert "$REPORT_PATH" evening >> "$REPO/finance_data/run_log.txt" 2>&1
    echo "추세 돌파 섹션 추가 종료 코드: $?" >> "$REPO/finance_data/run_log.txt"
fi
if [ "$CLAUDE_EXIT" -eq 0 ] && [ -s "$REPORT_PATH" ] && [ -n "$(git status --porcelain -- "$REPORT_PATH")" ]; then
    # 기존 private origin/push는 변경하지 않고, 오늘 보고서만 커밋한다.
    git add -- "$REPORT_PATH"
    git commit --only -m "자동 포스트마켓 리포트: $REPORT_DATE" -- "$REPORT_PATH" >> "$REPO/finance_data/run_log.txt" 2>&1
    # 원격(GitHub 웹 등)에서 직접 수정된 커밋이 있을 수 있어 push 전에 먼저 병합
    git pull --no-edit >> "$REPO/finance_data/run_log.txt" 2>&1
    git push >> "$REPO/finance_data/run_log.txt" 2>&1
    GIT_EXIT=$?
    echo "git 자동 커밋/푸시 종료 코드: $GIT_EXIT" >> "$REPO/finance_data/run_log.txt"
else
    echo "git 변경사항 없음 — 커밋 생략" >> "$REPO/finance_data/run_log.txt"
fi

# 완성된 리포트를 iCloud로 동기화되는 Obsidian 볼트에 복사해서 아이폰 Obsidian
# 앱에서 바로 열리게 한다. cwd가 아직 저장소 루트라 REPORT_PATH("finance_data/reports/...")가
# 그대로 유효하다 — finance_data로 cd한 뒤에 이 블록을 두면 경로가 어긋나므로 순서를 지킨다.
OBSIDIAN_VAULT_DIR="${OBSIDIAN_VAULT_DIR:-}"  # 선택: 리포트를 복사할 Obsidian 볼트 경로
if [ "$CLAUDE_EXIT" -eq 0 ] && [ -s "$REPORT_PATH" ] && [ -d "$OBSIDIAN_VAULT_DIR" ]; then
    cp "$REPORT_PATH" "$OBSIDIAN_VAULT_DIR/" >> "$REPO/finance_data/run_log.txt" 2>&1
    echo "Obsidian 볼트 복사 종료 코드: $?" >> "$REPO/finance_data/run_log.txt"
fi

# 디스코드는 파일 첨부 없이 "리포트 도착" 알림만 보낸다 — 실제 내용은 Obsidian에서 본다.
# ~/.discord_webhook_env에 DISCORD_WEBHOOK_URL이 없으면 조용히 건너뛴다 — 웹훅 설정은
# 선택 사항이라 없다고 파이프라인 전체를 실패로 처리하지 않는다.
if [ "$CLAUDE_EXIT" -eq 0 ] && [ -s "$REPORT_PATH" ]; then
    source ~/.discord_webhook_env > /dev/null 2>&1
    if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
        DISCORD_HTTP=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
            -H "Content-Type: application/json" \
            -d "{\"content\":\"📊 포스트마켓 리포트 도착: ${REPORT_DATE} — Obsidian에서 확인하세요\"}" \
            "$DISCORD_WEBHOOK_URL")
        echo "디스코드 알림 HTTP $DISCORD_HTTP" >> "$REPO/finance_data/run_log.txt"
    fi
fi
cd "$REPO/finance_data"

# 공개 미니앱(프리마켓 인사이트) 스냅샷 발행과 관심종목 가격 동기화는 GitHub Actions
# (premarket-insight-app .github/workflows/sync-stock-prices.yml)가 전담한다. 이 Mac은
# 더 이상 운영 Supabase에 공개 데이터를 쓰지 않는다 — 켜져 있어야 한다는 의존성을 없애기 위함
# (2026-09-07, 같은 스케줄에서 Mac과 GitHub Actions가 이중 발행하던 문제 정리).

# 맥 로컬 알림으로 성공/실패 요약 (무인 실행이라 폰 푸시는 안 가고 맥 화면에만 뜸)
if [ $PULL_EXIT -eq 0 ] && [ $FETCH_DATA_EXIT -eq 0 ] && [ $FETCH_KR_EXIT -eq 0 ] && [ $FETCH_SCRAPED_EXIT -eq 0 ] && \
   [ $FETCH_PROGRAM_EXIT -eq 0 ] && [ $CLAUDE_EXIT -eq 0 ] && [ $GIT_EXIT -eq 0 ]; then
    PIPELINE_EXIT=0
    osascript -e 'display notification "전 단계 성공, 리포트 생성 및 git 푸시 완료" with title "✅ 프리마켓 리포트 완료" sound name "Glass"' >> run_log.txt 2>&1
else
    PIPELINE_EXIT=1
    osascript -e "display notification \"pull:$PULL_EXIT fetch_data:$FETCH_DATA_EXIT fetch_kr:$FETCH_KR_EXIT fetch_scraped:$FETCH_SCRAPED_EXIT fetch_program:$FETCH_PROGRAM_EXIT claude:$CLAUDE_EXIT git:$GIT_EXIT\" with title \"⚠️ 프리마켓 리포트 실패\" sound name \"Basso\"" >> run_log.txt 2>&1
fi

echo "=== $(date) 실행 완료 ===" >> run_log.txt
exit "$PIPELINE_EXIT"
