"""15:10 종가배팅 체크: 장중 가격으로 추세 돌파 신호를 계산해 짧은 리포트로 저장한다 (LLM 없음).

- 오늘 종가 진입 후보 (구름대 위 + 20일 최고가 돌파, 등급은 전 거래일까지 수급으로 매긴 예비 등급 — 16:00에 당일 수급으로 다시 매김)
- 오늘 종가 기준 매도 예정 / 신호 진행 중 종목 (종가가 이틀 연속 매도선 아래면 매도 — 매도선은 10일선, 급등 종목은 5일선)
눌림목 '매수 유력' 판정은 백테스트상 우위가 없어 2026-10-21에 뺐다 — 종목별 조회(KRX 약 100회)도 함께 사라졌다.

종료 코드: 0 저장 완료, 3 휴장일/장 시작 전이라 생략, 1 실패.
"""
import os
import sys

from pykrx import stock

import breakout_signal as bs
from snapshot_validation import kst_now

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def main():
    now = kst_now()
    today = now.date().isoformat()
    market_today = stock.get_market_ohlcv(now.strftime('%Y%m%d'), market='KOSPI')
    if market_today.empty or market_today['거래대금'].sum() == 0:
        print('오늘 장중 데이터 없음 — 휴장일/장 시작 전 실행 생략')
        return 3

    breakout = bs.intraday_signal(today)
    bs.log_intraday(today, breakout['candidates'])

    lines = [
        f"# 🔔 종가배팅 체크 ({today} {now:%H:%M} 잠정)",
        "",
        f"- 조회 시각: {now:%Y-%m-%d %H:%M:%S} KST — 장중 가격이라 동시호가(15:20~15:30) 후 종가와 다를 수 있다",
        "- 이 리포트는 추세 돌파 신호만 다룬다. 매수 추천이 아니다.",
        "",
        bs.section("intraday", breakout),
        "",
    ]
    filename = f"{today}_closing_check.md"
    with open(f"{REPORT_DIR}/{filename}", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    b_names = ', '.join(c['name'] for c in breakout['candidates'])
    s_names = ', '.join(s['name'] for s in breakout['sells'])
    print(f"저장 완료: reports/{filename}")
    print(f"요약: 종가 진입 후보 {len(breakout['candidates'])}개{' (' + b_names + ')' if b_names else ''} / "
          f"종가 매도 예정 {len(breakout['sells'])}개{' (' + s_names + ')' if s_names else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
