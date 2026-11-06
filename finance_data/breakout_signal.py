"""추세 돌파 신호 (리포트의 주 신호).

사는 조건: 구름대(일목균형표) 위 + 종가가 직전 20거래일 최고가 돌파 → 15:10 종가 매수.
          외국인 매수 강도로 등급을 매긴다(16:00은 당일 수급까지, 15:10은 전 거래일까지로 매긴 예비 등급) — 1등급: 거래대금 상위 20 + 외국인 순매수 상위 10,
          2등급: 외국인 5거래일 누적 순매수, 3등급: 나머지. 대응표는 1·2등급, 3등급은 참고용 표에서 따로 추적한다.
파는 조건: 종가가 이틀 연속 10일 평균 가격(10일선) 아래로 마감하면 둘째 날 종가 매도 (최대 20거래일 보유).
          10일선은 주가를 따라 올라오므로, 오른 뒤 꺾이면 수익을 챙기고, 바로 빠지면 작게 잃고 나오는 '따라 올라가는 매도선'이다.
          단 신호 날 종가가 그날까지 9거래일 종가 평균보다 15% 이상 높았던(급등한) 종목은 10일선이 너무 멀어 5일선을 매도선으로 쓴다.

KRX 접속 제한(2026-10-20, 대량 조회로 하루 차단)을 다시 겪지 않으려고 종목별 조회 대신
시장 전체 하루치 시세·외국인 순매수(시장별 1회씩)만 받아 로컬 저장소(breakout_history.pkl)에 쌓는다.

사용법:
  python3 breakout_signal.py bootstrap <kr_cache.pkl>      # 최초 1회: 과거 데이터로 저장소 생성
  python3 breakout_signal.py insert <report.md> evening|morning   # 리포트 맨 위(제목 다음)에 섹션 삽입
16:00 수집은 fetch_data.py가 update_and_signal(), 15:10 체크는 closing_check.py가 intraday_signal()을 호출한다.
"""
import datetime
import json
import os
import pickle
import sys

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = f"{BASE}/breakout_history.pkl"
LOG_PATH = f"{BASE}/breakout_log.json"
MARKET_DATA_PATH = f"{BASE}/market_data.json"
FIELDS = ('Open', 'High', 'Low', 'Close', 'Volume', 'foreign', 'cap')
UNIVERSE = (("KOSPI", 100), ("KOSDAQ", 50))
KEEP = {"KOSPI": 200, "KOSDAQ": 100}  # 시총 상위 이 범위만 저장소에 남긴다(유니버스 변동 대비 여유분)
MIN_HISTORY = 80
SELL_LOOKBACK = 20
TRACK_MAX_TIER = 2  # 3등급까지 대응표에 넣으면 상승장에서 수십 줄로 늘어나, 3등급은 참고용 표에서 따로 따라간다
COST_PCT = 0.25  # 등급별 기록의 거래당 비용(백테스트와 같은 값)
# 16:00 등급(당일 수급)·지금 매도 규칙 기준, 2024-10~2025-09 / 2025-10~2026-09 (backtest_regime_result.md 13절)
BACKTEST_BY_TIER = {1: "+4.31% / +3.65%", 2: "+3.14% / +3.24%", 3: "+1.47% / +0.85%"}
SELL_CONFIRM_DAYS = 2  # 하루짜리 이탈은 다음 날 다시 10일선 위로 올라오는 경우가 약 25%라, 이틀 연속일 때만 판다
# 급등 종목은 10일선이 산 가격보다 한참 아래라 꺾여도 매도가 늦다. 백테스트(2024-10~2026-09)에서 여유 15% 이상 거래만 5일선으로 팔면
# 전체 거래당 평균은 그대로(+2.35%)였고, 그 거래들의 질 때 평균 손실은 -12.0% → -10.1%로 두 구간 모두 줄었다. 전부 5일선은 평균이 +1.93%로 낮아졌다.
FAST_EXIT_CUSHION = 15  # 신호 날 종가가 그날까지 9거래일 종가 평균보다 이만큼(%) 이상 높으면
FAST_EXIT_MA = 5        # 이 이평선을 매도선으로 쓴다 (아니면 10일선)

LEGEND = "\n".join([
    "**읽는 법**",
    "- 추세 돌파: 이미 오르는 흐름(구름대 위)에서 오늘 가격이 최근 20거래일 최고가를 넘어선 종목. "
    "'20일 최고가 돌파'는 '20일선(20일 평균 가격) 돌파'와 다르다 — 평균 가격 위로 올라오는 것보다 훨씬 드물고, 효과가 확인된 건 최고가 돌파 쪽이다.",
    "- 신호 진행 중은 새로 사는 신호가 아니다: 백테스트에서 신호 다음 날 이후 늦게 산 경우는 늦을수록 성과가 떨어졌고, 3거래일 이상 늦으면 아무 날 산 것과 차이가 없었다.",
    "- 등급 (외국인 매수 강도 — 대상 150종목 안에서 순위): 1등급 = 거래대금 상위 20위 + 외국인 순매수 상위 10위, "
    "2등급 = 1등급은 아니지만 외국인 5거래일 누적 순매수, 3등급 = 나머지. "
    "**16:00 리포트는 그날 수급까지(장 마감 직후라 잠정치), 15:10 체크는 장중에 그날 수급이 없어 전날까지로 매긴 예비 등급**이다 — 둘이 다르면 16:00 등급을 따른다. "
    "과거 거래당 평균(16:00 기준 등급, 비용 뺀 값, 2024-10~2025-09 / 2025-10~2026-09): "
    "1등급 +4.31% / +3.65%, 2등급 +3.14% / +3.24%, 3등급 +1.47% / +0.85%. 전날 수급으로 매기면 2등급과 3등급 성과가 거의 갈리지 않았다. "
    "백테스트는 15:30 종가에 산다고 계산했다 — 16:00 리포트를 보고 그 뒤 시간대에 사면 가격이 종가와 달라 이 수치와 달라진다. "
    "3등급은 대응표에 넣지 않고, 아래 '3등급 참고 추적' 표에서 같은 매도 기준으로 따로 따라간다(비교용 — 사는 대상이 아님).",
    "- 장 판단: 코스피가 60일선·20일선 위인지, 대상 150종목 중 20일선 위 종목이 절반 이상인지 세 가지로 본다. "
    "셋 다 위면 '좋음', 셋 다 아래면 '나쁨', 엇갈리면 '엇갈림'. 이 신호는 하락장(2018년, 2022년)에 손실이었기 때문에 같이 보라고 넣은 것이고, "
    "이 판정 기준 자체는 백테스트로 검증하지 않았다(참고용).",
    "- 사는 시점: 15:10 체크의 '오늘 종가 진입 후보'를 보고 그날 종가(동시호가)에 산다. 07:20 표는 산 종목을 오늘 어떻게 대응할지, "
    "16:00 표는 종가 확정 결과와 매도 신호를 보여준다.",
    "- 파는 기준: 종가가 **이틀 연속** 매도선 아래로 끝나면 둘째 날 종가에 판다. 하루만 아래면 아직 들고 간다. "
    "매도선은 보통 10일선(최근 10거래일 평균 가격)이고, **신호 날 급등해서 산 가격이 그날까지 9거래일 종가 평균보다 15% 이상 높았던 종목은 5일선**"
    "(최근 5거래일 평균 가격)이다. 종목마다 표의 '매도선'에 적혀 있다. "
    "장중에는 팔지 않는다. 산 다음 날부터 20거래일째(표의 보유 21일째) 종가에는 조건과 상관없이 판다. "
    "매도선은 주가를 따라 올라오기 때문에, 오르다 꺾이면 수익을 챙기고 나오고, 사자마자 빠지면 작게 잃고 나온다. "
    "'기준가'는 10일선이면 직전 9거래일, 5일선이면 직전 4거래일 종가 평균이다 — 그날 종가가 이보다 낮으면 그 순간 매도선 아래로 마감한 것이 된다.",
    "- 왜 급등 종목만 5일선인가: 급등한 날 사면 10일선이 산 가격보다 한참 아래라, 꺾여도 매도 신호가 늦게 나와 크게 잃는다. "
    "백테스트(2024-10~2026-09)에서 이런 거래만 5일선으로 팔았더니 질 때 평균 손실이 -12.0%에서 -10.1%로 두 구간 모두 줄었고, "
    "전체 거래당 평균은 +2.35%로 그대로였다(이런 거래만의 평균은 한 구간에선 늘고 한 구간에선 줄었다). "
    "평균 수익을 올리는 규칙이 아니라 크게 질 때 손실을 줄이는 규칙이다. 모든 종목을 5일선으로 팔면 큰 상승을 중간에 끊어 평균이 +1.93%로 낮아졌다.",
    "- 왜 이틀 연속인가: 10일선 아래로 한 번 마감한 종목 중 약 25%는 다음 날 바로 10일선 위로 돌아왔다. 하루 더 기다리면 이런 흔들림에 덜 털린다. "
    "대신 진짜로 꺾일 때는 하루 늦게 팔아 그만큼 더 잃는다.",
    "- 근거: 박스피였던 2015~2023년에도 '구름대 위 + 20일 최고가 돌파'는 3년 구간마다 거래당 +0.5~+1.4%(외국인 조건 없이)로 아무 날 산 것보다 나았고, "
    "이틀 연속일 때 파는 방식이 하루만 아래여도 파는 방식보다 다섯 구간 모두 나았다. "
    "이긴 비율은 38~43%뿐이다 — 열 번 중 예닐곱 번은 작게 잃고, 크게 오르는 몇 번이 수익을 만든다. "
    "하락장(2018년, 2022년)에는 손실이었다.",
    "- 신호 진행 중: 최근 후보를 신호대로 샀다고 가정했을 때 아직 매도 조건에 걸리지 않은 종목이다. 실제 계좌의 보유 종목이 아니다 — 사지 않았다면 무시하면 된다.",
    "- 표의 순서는 거래대금 순이며 우열이 아니다. 매수 추천이 아니다.",
])


def _empty_store():
    return {f: pd.DataFrame() for f in FIELDS} | {'names': {}, 'markets': {}}


def load_store():
    if not os.path.exists(STORE_PATH):
        return None
    with open(STORE_PATH, "rb") as f:
        return pickle.load(f)


def save_store(store):
    with open(STORE_PATH, "wb") as f:
        pickle.dump(store, f)


def bootstrap(cache_path):
    """백테스트용으로 받아둔 종목별 캐시를 저장소 형식으로 옮긴다 (KRX 조회 없음)."""
    with open(cache_path, "rb") as f:
        cache = pickle.load(f)
    store = _empty_store()
    cols = {f: {} for f in ('Open', 'High', 'Low', 'Close', 'Volume', 'foreign')}
    for code, (market, df) in cache.items():
        store['markets'][code] = market
        for f in cols:
            cols[f][code] = df[f]
    for f, d in cols.items():
        store[f] = pd.DataFrame(d).sort_index()
    store['cap'] = pd.DataFrame(index=store['Close'].index)
    save_store(store)
    return store


def fetch_day(date_str):
    """시장 전체 하루치 시세(시가총액 포함)·외국인 순매수. 시장별 2회씩, 총 4회 조회."""
    from pykrx import stock
    target = pd.to_datetime(date_str).strftime('%Y%m%d')
    rows, names, markets = {}, {}, {}
    for market, _ in UNIVERSE:
        o = stock.get_market_ohlcv(target, market=market)
        if o.empty or o['거래대금'].sum() == 0:
            raise ValueError(f'{market} {date_str} 시세 없음(휴장일/장 시작 전)')
        o = o.sort_values('시가총액', ascending=False).head(KEEP[market])
        f = stock.get_market_net_purchases_of_equities(target, target, market, "외국인")
        for code, r in o.iterrows():
            rows[code] = {'Open': r['시가'], 'High': r['고가'], 'Low': r['저가'], 'Close': r['종가'], 'Volume': r['거래량'],
                          'foreign': f['순매수거래대금'].get(code, 0) if not f.empty else float('nan'),
                          'cap': r['시가총액'], 'value': r['거래대금'], 'pct': r['등락률']}
            markets[code] = market
            if code in f.index:
                names[code] = f.at[code, '종목명']
    return pd.DataFrame(rows).T.apply(pd.to_numeric, errors='coerce'), names, markets


def append_day(store, date_str, day):
    ts = pd.Timestamp(date_str)
    for field in FIELDS:
        frame = store[field]
        frame = frame.reindex(columns=frame.columns.union(day.index))
        frame.loc[ts] = day[field].reindex(frame.columns)
        store[field] = frame.sort_index().astype(float)


def _universe(day, markets):
    picked = []
    for market, n in UNIVERSE:
        sub = day[[markets.get(c) == market for c in day.index]]
        picked += list(sub.sort_values('cap', ascending=False).head(n).index)
    return picked


def _flow_ranks(store, ts, codes, same_day):
    """대상 종목 안의 외국인 순매수 순위와 거래대금(종가×거래량) 순위 — same_day면 ts 당일, 아니면 전 거래일."""
    days = store['Close'].index[store['Close'].index <= ts] if same_day else store['Close'].index[store['Close'].index < ts]
    if not len(days):
        return pd.Series(dtype=float), pd.Series(dtype=float)
    day = days[-1]
    cols = [c for c in codes if c in store['Close'].columns]
    f = store['foreign'].loc[day, cols]
    v = store['Close'].loc[day, cols] * store['Volume'].loc[day, cols]
    return f.rank(ascending=False), v.rank(ascending=False)


def compute(store, date_str, codes, same_day_flows):
    """date_str 기준 신호. 등급(외국인 조건)은 same_day_flows면 당일까지(16:00, 당일 수급은 잠정치),
    아니면 전 거래일까지(15:10 — 장중에는 당일 수급이 없어 예비 등급) 데이터로 매긴다.
    백테스트(2024-10~2026-09)에서 당일 수급으로 매긴 등급이 1·2등급과 3등급을 두 구간 모두 더 잘 갈랐다."""
    import fetch_data as fd
    ts = pd.Timestamp(date_str)
    frames = {f: store[f] for f in ('Open', 'High', 'Low', 'Close', 'Volume', 'foreign')}
    f_rank, v_rank = _flow_ranks(store, ts, codes, same_day_flows)
    out, skipped = [], 0
    for code in codes:
        if code not in frames['Close'].columns:
            skipped += 1
            continue
        df = pd.DataFrame({f: frames[f][code] for f in frames}).loc[:ts].dropna(subset=['Close'])
        if len(df) < MIN_HISTORY or df.index[-1] != ts:
            skipped += 1
            continue
        df = fd.calculate_ichimoku(df.tail(140).astype(float).copy())
        c = df['Close'].iat[-1]
        cloud_top = max(df['span_a'].iat[-1], df['span_b'].iat[-1])
        prev_high20 = df['High'].iloc[-21:-1].max()
        flows = df['foreign'].iloc[-5:] if same_day_flows else df['foreign'].iloc[-6:-1]
        if pd.isna(cloud_top):
            continue
        if c > cloud_top and c > prev_high20:
            n_ma = exit_ma(df['Close'], ts)
            fr, vr = f_rank.get(code), v_rank.get(code)
            if pd.notna(fr) and pd.notna(vr) and fr <= 10 and vr <= 20:
                tier = 1
            elif not flows.isna().any() and flows.sum() > 0:
                tier = 2
            else:
                tier = 3
            out.append({'code': code, 'name': store['names'].get(code, code), 'market': store['markets'].get(code),
                        'tier': tier, 'flow_basis': '당일' if same_day_flows else '전날',
                        'f_rank': None if pd.isna(fr) else int(fr), 'v_rank': None if pd.isna(vr) else int(vr),
                        'close': int(c), 'pct_change': round((c / df['Close'].iat[-2] - 1) * 100, 2),
                        'prev_high20': int(prev_high20), 'ma10': int(round(df['Close'].tail(10).mean())),
                        'ma_n': n_ma, 'sell_below_next': int(round(df['Close'].tail(n_ma - 1).mean())),
                        'foreign_5d_eok': None if flows.isna().any() else round(flows.sum() / 1e8, 1),
                        'value_eok': round(float(df['Close'].iat[-1] * df['Volume'].iat[-1]) / 1e8, 0)})
    out.sort(key=lambda r: (r['tier'], -r['value_eok']))
    return out, skipped


def _kospi_trend(date_str):
    """코스피 지수 종가와 20·60일선 (지수 일봉 1회 조회 — 종목별 조회가 아니라 접속 제한 걱정 없음). 실패하면 None."""
    try:
        from pykrx import stock
        end = pd.Timestamp(date_str)
        df = stock.get_index_ohlcv((end - pd.Timedelta(days=140)).strftime('%Y%m%d'), end.strftime('%Y%m%d'), '1001')
        c = df['종가'].astype(float)
        if len(c) < 60:
            return None
        return {'date': c.index[-1].strftime('%Y-%m-%d'), 'close': round(c.iat[-1], 2),
                'ma20': round(c.tail(20).mean(), 2), 'ma60': round(c.tail(60).mean(), 2)}
    except Exception as e:
        print(f"코스피 지수 조회 실패: {e}")
        return None


def _breadth(store, ts, codes):
    """대상 종목 중 종가가 자기 20일선 위인 비율(%) — 오늘과 5거래일 전."""
    cols = [c for c in codes if c in store['Close'].columns]
    close = store['Close'].loc[:ts, cols]
    if len(close) < 26:
        return None
    above = (close > close.rolling(20).mean()).where(close.rolling(20).mean().notna())

    def pct(row):
        row = row.dropna()
        return round(float(row.mean()) * 100, 1) if len(row) else None
    return {'pct': pct(above.iloc[-1]), 'pct_5d_ago': pct(above.iloc[-6]), 'n': int(above.iloc[-1].notna().sum())}


def market_regime(store, date_str, codes, cands):
    """장 판단: 코스피 60일선·20일선, 대상 종목 20일선 위 비율 — 셋 다 위면 좋음, 셋 다 아래면 나쁨, 엇갈리면 엇갈림."""
    kospi = _kospi_trend(date_str)
    breadth = _breadth(store, pd.Timestamp(date_str), codes)
    checks = []
    if kospi:
        checks += [kospi['close'] > kospi['ma60'], kospi['close'] > kospi['ma20']]
    if breadth and breadth['pct'] is not None:
        checks.append(breadth['pct'] >= 50)
    if len(checks) < 2:
        verdict = "확인불가"
    elif all(checks):
        verdict = "좋음"
    elif not any(checks):
        verdict = "나쁨"
    else:
        verdict = "엇갈림"
    by_tier = {t: sum(1 for c in cands if c['tier'] == t) for t in (1, 2, 3)}
    return {'verdict': verdict, 'kospi': kospi, 'breadth': breadth, 'cands_by_tier': by_tier}


def tier_record(store, ts):
    """로그에 쌓인 모든 후보(15:10 + 16:00)를 신호일 종가에 샀다고 보고 등급별 성과를 모은다.
    같은 종목은 들고 있는 동안 나온 새 신호를 무시한다(_positions와 같은 방식). 끝난 거래만 평균에 넣고 비용 COST_PCT를 뺀다."""
    close = store['Close']
    signals = {}
    for path in (LOG_PATH, LOG_1510_PATH):
        for d_str, items in _load_json(path).items():
            d = pd.Timestamp(d_str)
            if d <= ts:
                for item in items:
                    signals.setdefault(item['code'], {}).setdefault(d, item)
    rec = {}
    for code, by_day in signals.items():
        if code not in close.columns:
            continue
        s = close[code].loc[:ts].dropna()
        held_until = None
        for d in sorted(by_day):
            if (held_until is not None and d <= held_until) or d not in s.index:
                continue
            r = rec.setdefault(by_day[d].get('tier'), {'pnl': [], 'open': 0, 'since': d})
            r['since'] = min(r['since'], d)
            exit_day, _, exit_px = _follow(store, code, d, ts)
            if exit_day is None:
                r['open'] += 1
                break
            r['pnl'].append((exit_px / float(s.loc[d]) - 1) * 100 - COST_PCT)
            held_until = exit_day
    out = []
    for tier in sorted(rec, key=lambda t: (t is None, t or 0)):
        r = rec[tier]
        n = len(r['pnl'])
        out.append({'tier': tier, 'closed': n, 'open': r['open'], 'since': r['since'].strftime('%Y-%m-%d'),
                    'avg_pct': round(sum(r['pnl']) / n, 2) if n else None,
                    'win_pct': round(sum(p > 0 for p in r['pnl']) / n * 100) if n else None})
    return out


def _main_tier(tier):
    """대응표(1·2등급)에 넣는 종목인지. 등급을 기록하기 전 로그(tier 없음)는 외국인 5일 순매수 조건이라 대응표 쪽이다."""
    return tier is None or tier <= TRACK_MAX_TIER


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _positions(store, ts):
    """최근 후보(16:00 확정 + 15:10 장중)로 '신호대로 샀다면'의 포지션을 백테스트와 같은 방식으로 재구성한다:
    들고 있는 동안 나온 같은 종목의 새 신호는 무시하고, 판 뒤에 나온 신호부터 새로 산다.
    반환: {code: (산 날, 후보 항목)} — ts 기준으로 아직 들고 있거나 ts에 판 포지션만."""
    close = store['Close']
    past = close.index[close.index < ts]
    if not len(past):
        return {}
    window_start = past[-2 * SELL_LOOKBACK] if len(past) >= 2 * SELL_LOOKBACK else past[0]
    signals = {}
    for path in (LOG_PATH, LOG_1510_PATH):
        for d_str, items in _load_json(path).items():
            d = pd.Timestamp(d_str)
            if window_start <= d < ts:
                for item in items:
                    signals.setdefault(item['code'], {}).setdefault(d, item)  # 같은 날이면 16:00(당일 수급) 등급을 따른다
    out = {}
    for code, by_day in signals.items():
        if code not in close.columns:
            continue
        held_until = None
        for d in sorted(by_day):
            if held_until is not None and d <= held_until:
                continue
            exit_day, _, _ = _follow(store, code, d, ts)
            if exit_day is None or exit_day == ts:
                out[code] = (d, by_day[d])
                break
            held_until = exit_day
    return out


def exit_ma(s, d):
    """신호일 d에 산 종목의 매도선이 몇 일선인지. 신호 날 종가가 그날까지 9거래일 종가 평균보다 FAST_EXIT_CUSHION% 이상 높으면 FAST_EXIT_MA."""
    upto = s.loc[:d].dropna()
    if len(upto) < 9:
        return 10
    return FAST_EXIT_MA if (upto.iat[-1] / upto.tail(9).mean() - 1) * 100 >= FAST_EXIT_CUSHION else 10


def _follow(store, code, d, ts):
    """신호일 d 종가에 샀다고 보고 ts까지 하루씩 따라간다. 반환: (매도일, 이유, 매도가) 또는 (None, None, None).
    종가가 이틀 연속 매도선(10일선, 급등 종목은 5일선) 아래면 둘째 날 종가 매도, 20거래일이 되면 종가 매도."""
    close = store['Close'][code].loc[:ts].dropna()
    n_ma = exit_ma(close, d)
    ma = close.rolling(n_ma).mean()
    below = 0
    for n, day in enumerate(close.index[close.index > d], 1):
        below = below + 1 if close[day] < ma[day] else 0
        if below >= SELL_CONFIRM_DAYS:
            return day, f'{SELL_CONFIRM_DAYS}일 연속 종가 {n_ma}일선 아래 → 매도', float(close[day])
        if n >= SELL_LOOKBACK:
            return day, f'{SELL_LOOKBACK}거래일 경과 → 종가 매도', float(close[day])
    return None, None, None


def _below_streak(s, n_ma=10):
    """마지막 날까지 종가가 n_ma일선 아래로 연속 마감한 날 수."""
    ma = s.rolling(n_ma).mean()
    streak = 0
    for c, m in zip(reversed(s.values), reversed(ma.values)):
        if pd.isna(m) or c >= m:
            break
        streak += 1
    return streak


def track_positions(store, date_str):
    """최근 후보를 신호일 종가에 샀다고 가정하고 따라가, 오늘 매도된 종목(sells)과 아직 들고 있는 종목(holds)을 나눈다.
    예전에 이미 매도 조건에 걸린 종목은 빼서, 오래된 후보가 '오늘 매도'로 잘못 보이지 않게 한다."""
    ts = pd.Timestamp(date_str)
    close = store['Close']
    if ts not in close.index:
        return [], []
    sells, holds = [], []
    for code, (d, item) in sorted(_positions(store, ts).items(), key=lambda kv: kv[1][0]):
        if code not in close.columns:
            continue
        s = close[code].loc[:ts].dropna()
        if d not in s.index:
            continue
        entry = float(s.loc[d])
        exit_day, reason, exit_px = _follow(store, code, d, ts)
        n_ma = exit_ma(s, d)
        row = {'name': item['name'], 'code': code, 'tier': item.get('tier'), 'signal_date': d.strftime('%Y-%m-%d'), 'entry': int(round(entry)),
               'close': int(s.iat[-1]), 'ma_n': n_ma, 'streak': _below_streak(s, n_ma), 'prev_streak': _below_streak(s.iloc[:-1], n_ma),
               'sell_below_today': int(round(s.iloc[-n_ma:-1].mean())), 'reason': reason}
        if exit_day == ts:
            row['exit_price'] = int(round(exit_px))
            row['pnl_pct'] = round((exit_px / entry - 1) * 100, 2)
            sells.append(row)
        elif exit_day is None:
            row['pnl_pct'] = round((s.iat[-1] / entry - 1) * 100, 2)
            holds.append(row)
    return sells, holds


LOG_1510_PATH = f"{BASE}/breakout_log_1510.json"


def log_intraday(date_str, cands):
    """15:10 후보(사용자가 그날 종가에 실제로 살 수 있었던 목록)를 따로 남겨 다음 날 대응표와 이후 추적에 쓴다."""
    log = _load_json(LOG_1510_PATH)
    log[date_str] = [{'code': c['code'], 'name': c['name'], 'close': c['close'], 'tier': c['tier']} for c in cands]
    with open(LOG_1510_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)


def next_day_plan(store, date_str, today_cands, holds, sells=()):
    """16:00에 계산해 두는 '다음 거래일 대응표': 신호 진행 중 종목 + 오늘 15:10 후보(오늘 종가 매수분) + 오늘 종가 확정 후보.
    들고 있는 동안(오늘 판 종목 포함) 같은 종목에 새 신호가 나와도 새로 사지 않는다 — 백테스트·_positions와 같은 규칙.
    3등급도 함께 계산하고(tier로 구분), 리포트에서 대응표와 참고 추적 표로 나눠 보여준다.
    다음 거래일 종가가 '오늘까지 9거래일(5일선 종목은 4거래일) 종가 평균'보다 낮으면 매도선 아래 마감 — 오늘 이미 아래였으면(이틀 연속) 매도."""
    ts = pd.Timestamp(date_str)
    close = store['Close']
    positions = {h['code']: {'name': h['name'], 'code': h['code'], 'tier': h['tier'], 'source': '신호 진행 중', 'signal_date': h['signal_date']}
                 for h in holds}
    held = set(positions) | {s['code'] for s in sells}
    for c in _load_json(LOG_1510_PATH).get(date_str, []):
        if c['code'] not in held:
            positions[c['code']] = {'name': c['name'], 'code': c['code'], 'tier': c.get('tier'), 'source': '15:10 후보 (그날 종가 매수분)', 'signal_date': date_str}
    for c in today_cands:
        if c['code'] in held:
            continue
        positions.setdefault(c['code'], {'name': c['name'], 'code': c['code'], 'tier': c['tier'], 'source': '종가 확정 후보', 'signal_date': date_str})
        positions[c['code']]['tier'] = c['tier']  # 15:10 예비 등급보다 16:00(당일 수급) 등급을 따른다
    plan = []
    for p in positions.values():
        if p['code'] not in close.columns:
            continue
        s = close[p['code']].loc[:ts].dropna()
        sig = pd.Timestamp(p['signal_date'])
        if sig not in s.index:
            continue
        entry = float(s.loc[sig])
        held = len(s.index[s.index > sig])
        n_ma = exit_ma(s, sig)
        plan.append({**p, 'entry': int(round(entry)), 'close': int(s.iat[-1]), 'ma_n': n_ma,
                     'sell_below_next': int(round(s.iloc[-(n_ma - 1):].mean())), 'streak': _below_streak(s, n_ma),
                     'pnl_pct': round((s.iat[-1] / entry - 1) * 100, 2),
                     'held_days': held, 'force_exit_next': held + 1 >= SELL_LOOKBACK,
                     'in_close_confirmed': any(c['code'] == p['code'] for c in today_cands)})
    return plan


def fetch_final_flows(date_str):
    """전 거래일 외국인·기관 순매수 확정치(시장 전체 한 번에). 시장별 2회, 총 4회 조회."""
    from pykrx import stock
    target = pd.to_datetime(date_str).strftime('%Y%m%d')
    out = {}
    for market, _ in UNIVERSE:
        for investor, key in (("외국인", "foreign"), ("기관합계", "inst")):
            f = stock.get_market_net_purchases_of_equities(target, target, market, investor)
            if f.empty:
                raise ValueError(f'{market} {investor} {date_str} 수급 없음')
            for code, value in f['순매수거래대금'].items():
                out.setdefault(code, {})[key] = float(value)
    return out


def morning_refresh(result):
    """07:20: 전 거래일 확정 수급으로 대응표를 보강하고, 저장소의 그날 외국인 값을 확정치로 바꾼다
    (16:00 수집값은 잠정치라 저녁에 바뀌는 것을 확인함 — 2026-10-20 메리츠금융지주)."""
    store = load_store()
    ts = pd.Timestamp(result['date'])
    flows = fetch_final_flows(result['date'])
    foreign = store['foreign']
    if ts in foreign.index:
        for code, v in flows.items():
            if code in foreign.columns and 'foreign' in v:
                foreign.at[ts, code] = v['foreign']
        save_store(store)
    for p in result.get('plan', []):
        v = flows.get(p['code'], {})
        p['foreign_1d_eok'] = round(v['foreign'] / 1e8, 1) if 'foreign' in v else None
        p['inst_1d_eok'] = round(v['inst'] / 1e8, 1) if 'inst' in v else None
        if p['code'] in foreign.columns:
            p['foreign_5d_eok'] = round(float(foreign[p['code']].loc[:ts].dropna().tail(5).sum()) / 1e8, 1)
    result['flows_final'] = True
    return result


def _write_log(date_str, cands):
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = {}
    log[date_str] = [{'code': c['code'], 'name': c['name'], 'close': c['close'], 'tier': c['tier']} for c in cands]
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)


def update_and_signal(date_str):
    """16:00: 오늘 확정 데이터를 저장소에 추가하고, 오늘 종가 기준 신호(등급은 오늘 수급까지 — 16:00 수집값은 잠정치)."""
    store = load_store()
    if store is None:
        raise RuntimeError('breakout_history.pkl 없음 — bootstrap 먼저 실행')
    day, names, markets = fetch_day(date_str)
    store['names'].update(names)
    store['markets'].update(markets)
    append_day(store, date_str, day)
    save_store(store)
    codes = _universe(day, store['markets'])
    cands, skipped = compute(store, date_str, codes, same_day_flows=True)
    _write_log(date_str, cands)
    sells, holds = track_positions(store, date_str)
    return {'date': date_str, 'candidates': cands, 'skipped_no_history': skipped, 'sells': sells, 'holds': holds,
            'plan': next_day_plan(store, date_str, cands, holds, sells), 'regime': market_regime(store, date_str, codes, cands),
            'record': tier_record(store, pd.Timestamp(date_str))}


def intraday_signal(date_str):
    """15:10: 장중 가격으로 오늘 종가 진입 후보와 '오늘 종가 기준 매도 예정' 종목.
    장중 가격은 저장소에 쓰지 않고(16:00 확정치만 저장) 메모리에서만 붙인다. 오늘 외국인 수급은 장중에 없으므로 등급은 전 거래일까지로 매긴 예비 등급이다(16:00에 당일 수급으로 다시 매김)."""
    store = load_store()
    day, names, markets = fetch_day(date_str)
    temp = {f: store[f].copy() for f in FIELDS}
    temp['names'] = {**store['names'], **names}
    temp['markets'] = {**store['markets'], **markets}
    append_day(temp, date_str, day)
    codes = _universe(day, temp['markets'])
    cands, skipped = compute(temp, date_str, codes, same_day_flows=False)
    sells, holds = track_positions(temp, date_str)
    return {'date': date_str, 'candidates': cands, 'skipped_no_history': skipped, 'sells': sells, 'holds': holds,
            'regime': market_regime(temp, date_str, codes, cands)}


def _ma_label(row):
    n = row.get('ma_n', 10)
    return "10일선" if n == 10 else f"{n}일선 (급등 종목)"


def table(cands, with_sell_level=True):
    if not cands:
        return "해당 종목 없음"
    basis = cands[0].get('flow_basis', '전날')
    head = (f"| 등급 | 종목 | 시장 | 가격 | 돌파한 20일 최고가 | {basis} 외국인 순위 / 거래대금 순위 | 외국인 5일 순매수 ({basis}까지) | "
            "매도선 / 다음 날 기준가 (종가) | 거래대금 |")
    lines = [head, "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"]
    for c in cands:
        tier = f"{c['tier']}등급" + ("" if _main_tier(c['tier']) else " (참고)")
        ranks = f"{c.get('f_rank') or '-'}위 / {c.get('v_rank') or '-'}위"
        f5 = "확인불가" if c['foreign_5d_eok'] is None else f"{c['foreign_5d_eok']:+,.1f}억"
        lines.append(f"| {tier} | {c['name']} | {c['market']} | {c['close']:,} ({c['pct_change']:+.2f}%) | {c['prev_high20']:,} | {ranks} | "
                     f"{f5} | {_ma_label(c)} — 종가 {c['sell_below_next']:,} 아래면 1일째 (이틀 연속이면 매도) | {c['value_eok']:,.0f}억 |")
    return "\n".join(lines)


def sell_table(rows, empty_text):
    """오늘 매도된(또는 15:10 기준 매도 예정인) 종목."""
    if not rows:
        return empty_text
    lines = ["| 종목 | 산 날 | 산 가격 | 매도 가격 | 수익률 | 이유 |", "| :--- | :--- | :--- | :--- | :--- | :--- |"]
    for s in rows:
        lines.append(f"| {s['name']} | {s['signal_date']} | {s['entry']:,} | {s['exit_price']:,} | {s['pnl_pct']:+.2f}% | {s['reason']} |")
    return "\n".join(lines)


def hold_table(rows, empty_text):
    """신호 진행 중(아직 매도 조건에 안 걸린) 종목 — 15:10 장중 기준."""
    if not rows:
        return empty_text
    lines = ["| 종목 | 산 날 | 산 가격 | 지금 가격 | 수익률 | 매도선 | 오늘 기준가 (종가) | 상태 |",
             "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"]
    for h in rows:
        ma = f"{h.get('ma_n', 10)}일선"
        if h['prev_streak'] >= 1:
            state = f"⚠️ 어제 {ma} 아래 마감 — 오늘 종가가 기준가 아래면 매도"
        elif h['streak'] >= 1:
            state = "지금 가격이 기준가 아래 — 이대로 마감하면 1일째, 내일도 아래면 매도"
        else:
            state = f"{ma} 위"
        lines.append(f"| {h['name']} | {h['signal_date']} | {h['entry']:,} | {h['close']:,} | {h['pnl_pct']:+.2f}% | "
                     f"{_ma_label(h)} | {h['sell_below_today']:,} | {state} |")
    return "\n".join(lines)


def plan_table(plan, base_date, day_word, empty_text="대응할 종목 없음 (최근 추세 돌파 후보·신호 진행 중인 종목이 없음)"):
    """day_word: 대응하는 날을 부르는 말 ('오늘' — 07:20, '내일' — 16:00)."""
    if not plan:
        return empty_text
    with_flows = any('foreign_1d_eok' in p for p in plan)
    head = f"| 종목 | 구분 | 산 날 | 산 가격 | {base_date} 종가 (수익률) | {day_word} 대응 (종가 기준) | 보유 (산 날 = 1일째) |"
    sep = "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
    if with_flows:
        head += f" {base_date} 외국인 (확정) | {base_date} 기관 (확정) | 외국인 5일 누적 (확정) |"
        sep += " :--- | :--- | :--- |"
    lines = [head, sep]

    def eok(v):
        return "확인불가" if v is None else f"{v:+,.1f}억"

    for p in plan:
        if p['force_exit_next']:
            sell = f"산 다음 날부터 {SELL_LOOKBACK}거래일째 — 종가에 매도"
        elif p['streak'] >= 1:
            sell = f"⚠️ {base_date} {p.get('ma_n', 10)}일선 아래 마감(1일째) — 종가 {p['sell_below_next']:,} 아래면 매도(2일째), 위면 보유"
        else:
            sell = f"종가 {p['sell_below_next']:,} 아래면 1일째(보유), 위면 보유"
        source = p['source'] + ("" if p['in_close_confirmed'] or p['source'] == '신호 진행 중' else " — ⚠️ 종가 기준으론 돌파 신호가 아니었음")
        if with_flows and p.get('foreign_5d_eok') is not None and p['foreign_5d_eok'] <= 0:
            source += " — ⚠️ 외국인 5일 누적 순매도 (사는 조건 깨짐)"
        line = (f"| {p['name']} | {source} | {p['signal_date']} | {p['entry']:,} | {p['close']:,} ({p['pnl_pct']:+.2f}%) | "
                f"{_ma_label(p)} — {sell} | {p['held_days'] + 1}일째 |")
        if with_flows:
            line += f" {eok(p.get('foreign_1d_eok'))} | {eok(p.get('inst_1d_eok'))} | {eok(p.get('foreign_5d_eok'))} |"
        lines.append(line)
    return "\n".join(lines)


REGIME_ICON = {"좋음": "🟢", "엇갈림": "🟡", "나쁨": "🔴", "확인불가": "⚪"}


def regime_block(regime, when):
    """장 판단 표. when: 판정 기준 시점을 부르는 말."""
    if not regime:
        return "### 📊 장 판단\n\n확인불가 (이전 버전에서 저장된 데이터)"
    k, b, n = regime.get('kospi'), regime.get('breadth'), regime.get('cands_by_tier', {})

    def side(v, ma):
        return "위 ✅" if v > ma else "아래 ❌"
    lines = [f"### 📊 장 판단 ({when}): {REGIME_ICON.get(regime['verdict'], '')} {regime['verdict']}", "",
             "| 지표 | 값 | 판정 |", "| :--- | :--- | :--- |"]
    if k:
        lines.append(f"| 코스피 vs 60일선 (석 달 흐름) | {k['close']:,.2f} / 60일선 {k['ma60']:,.2f} ({k['date']}) | {side(k['close'], k['ma60'])} |")
        lines.append(f"| 코스피 vs 20일선 (한 달 흐름) | {k['close']:,.2f} / 20일선 {k['ma20']:,.2f} | {side(k['close'], k['ma20'])} |")
    else:
        lines.append("| 코스피 vs 60일선·20일선 | 확인불가 (지수 조회 실패) | - |")
    if b and b.get('pct') is not None:
        prev = f", 5거래일 전 {b['pct_5d_ago']}%" if b.get('pct_5d_ago') is not None else ""
        lines.append(f"| 대상 {b['n']}종목 중 20일선 위 비율 | {b['pct']}%{prev} | {'절반 이상 ✅' if b['pct'] >= 50 else '절반 미만 ❌'} |")
    else:
        lines.append("| 대상 종목 중 20일선 위 비율 | 확인불가 | - |")
    lines.append(f"| 추세 돌파 후보 수 (참고) | 1등급 {n.get(1, 0)} · 2등급 {n.get(2, 0)} · 3등급 {n.get(3, 0)} | - |")
    lines += ["", "- 셋 다 위면 좋음, 셋 다 아래면 나쁨, 엇갈리면 엇갈림. 판정 기준 자체는 백테스트로 검증하지 않은 참고용이다."]
    return "\n".join(lines)


def record_table(record):
    """등급별 실전 기록: 로그가 쌓이기 시작한 날부터 신호대로 샀다고 가정한 결과."""
    if not record:
        return "아직 기록 없음"
    lines = ["| 등급 | 기록 시작 | 끝난 거래 | 거래당 평균 (비용 0.25% 뺀 값) | 이긴 비율 | 진행 중 | 백테스트 거래당 평균 |",
             "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |"]
    for r in record:
        label = "등급 기록 전 (1·2등급 해당)" if r['tier'] is None else f"{r['tier']}등급"
        avg = "-" if r['avg_pct'] is None else f"{r['avg_pct']:+.2f}%"
        win = "-" if r['win_pct'] is None else f"{r['win_pct']}%"
        lines.append(f"| {label} | {r['since']} | {r['closed']}건 | {avg} | {win} | {r['open']}건 | {BACKTEST_BY_TIER.get(r['tier'], '-')} |")
    return "\n".join(lines)


def _split(rows):
    return [r for r in rows if _main_tier(r.get('tier'))], [r for r in rows if not _main_tier(r.get('tier'))]


def section(mode, result):
    if not result:
        return "## 🚀 추세 돌파 (주 신호)\n\n추세 돌파 데이터 확인불가 — 전 거래일 16:00 수집이 실패했을 수 있음"
    skipped = f" (과거 데이터가 80거래일 미만이라 제외된 종목 {result['skipped_no_history']}개)" if result.get('skipped_no_history') else ""
    sells, sells3 = _split(result.get('sells', []))
    holds, holds3 = _split(result.get('holds', []))
    plan, plan3 = _split(result.get('plan', []))
    ref_note = "- 3등급은 사는 대상이 아니라, 1·2등급과 비교해 보라고 같은 매도 기준으로 따라가는 표다."
    if mode == "morning":
        title = f"## 🚀 오늘 대응할 추세 돌파 종목 (전 거래일 {result['date']} 종가 기준)"
        body = [regime_block(result.get('regime'), f"전 거래일 {result['date']} 종가"), "",
                LEGEND, "", plan_table(plan, result['date'], '오늘'), "",
                "- 이 표는 신호대로 15:10에 샀다고 가정한 종목이다. 실제 계좌는 모르기 때문에, 사지 않은 종목은 무시하면 된다.",
                "- 장중에는 팔지 않는다. 종가가 이틀 연속 기준가 아래로 끝날 때만 판다(15:10 체크에서 지금 가격 기준으로 미리 알려준다).",
                ("- 수급은 전 거래일 장 마감 후 최종 확정된 값이다(16:00 리포트의 잠정치와 조금 다를 수 있다). "
                 "'외국인 5일 누적 순매도'는 사는 조건 중 하나가 지금은 깨졌다는 뜻이다. 다만 "
                 "이걸 이유로 미리 파는 게 나은지는 검증하지 않았다(백테스트의 파는 기준은 이틀 연속 종가 매도선 이탈뿐)."
                 if result.get('flows_final') else "- 확정 수급 조회에 실패해 수급 칸은 빠졌다."),
                "- '⚠️ 종가 기준으론 돌파 신호가 아니었음'은 15:10에는 후보였지만 동시호가 후 종가로는 조건이 깨진 종목이다. 이미 샀다면 같은 기준으로 대응한다.",
                "", "### 3등급 참고 추적 (사는 대상 아님)", "",
                plan_table(plan3, result['date'], '오늘', "추적 중인 3등급 없음"), "", ref_note]
    elif mode == "intraday":
        title = f"## 🚀 추세 돌파 — 오늘 종가 진입 후보와 대응 ({result['date']} 장중 가격 기준)"
        body = [regime_block(result.get('regime'), "장중 가격"), "",
                LEGEND, "", "### 오늘 종가 진입 후보", "", table(result['candidates']) + skipped,
                "", "- 장중 가격 기준이라 동시호가 후 종가가 20일 최고가 아래로 끝나면 신호가 사라진다. 등급은 장중에 그날 수급이 없어 전 거래일까지로 매긴 **예비 등급**이다 — 16:00 리포트에서 그날 수급(잠정치)으로 다시 매기고, 다르면 16:00 등급을 따른다.", "",
                "### 오늘 매도 예정 (어제 매도선 아래로 마감했고 지금 가격도 기준가 아래이거나, 20거래일이 된 종목)", "",
                sell_table(sells, "오늘 매도 대상 없음"),
                "", "- 지금 가격 기준 '예정'이다. 종가가 기준가 위로 회복해서 끝나면 매도 조건이 사라진다.", "",
                "### 신호 진행 중 (신호대로 샀다면 아직 팔 때가 안 된 종목 — 실제 보유 종목이 아님)", "",
                hold_table(holds, "없음"), "",
                "### 3등급 참고 추적 (사는 대상 아님)", "",
                "**오늘 매도 예정**", "", sell_table(sells3, "없음"), "",
                "**신호 진행 중**", "", hold_table(holds3, "없음"), "", ref_note]
    else:
        title = f"## 🚀 추세 돌파 (주 신호, {result['date']} 종가 확정)"
        body = [regime_block(result.get('regime'), f"{result['date']} 종가"), "",
                LEGEND, "", "### 오늘 종가 확정 추세 돌파 (15:10에 샀다면 확인용)", "", table(result['candidates']) + skipped, "",
                "### 오늘 매도 결과 (종가가 이틀 연속 매도선 아래로 마감했거나 20거래일이 된 종목)", "",
                sell_table(sells, "오늘 매도된 종목 없음"), "",
                "### 내일 대응 기준 (내일 07:20 브리핑에도 같은 표가 나온다)", "",
                plan_table(plan, result['date'], '내일'), "",
                "### 3등급 참고 추적 (사는 대상 아님)", "",
                "**오늘 매도 결과**", "", sell_table(sells3, "없음"), "",
                "**내일 기준**", "", plan_table(plan3, result['date'], '내일', "추적 중인 3등급 없음"), "", ref_note, "",
                "### 등급별 실전 기록 (로그가 쌓인 날부터, 신호대로 샀다고 가정)", "",
                record_table(result.get('record')), "",
                "- 15:10 후보였지만 종가로는 깨진 종목도 그날 살 수 있었던 종목이라 포함한다. 끝난 거래가 적을 때는 평균이 크게 흔들리니 백테스트 값과 방향만 비교한다."]
    return title + "\n\n" + "\n".join(body)


def insert_after_title(report_path, text):
    with open(report_path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    pos = next((i + 1 for i, line in enumerate(lines) if line.startswith("# ")), 0)
    lines[pos:pos] = ["", text, "", "---", ""]
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    cmd = sys.argv[1]
    if cmd == "bootstrap":
        store = bootstrap(sys.argv[2])
        print(f"저장소 생성: 종목 {store['Close'].shape[1]}개, {store['Close'].index.min().date()}~{store['Close'].index.max().date()}")
        return 0
    if cmd == "backfill":
        # 캐시 마지막 날 이후 빠진 거래일을 시장 전체 조회(하루 4회)로 채운다.
        store = load_store()
        for date_str in sys.argv[2:]:
            day, names, markets = fetch_day(date_str)
            store['names'].update(names)
            store['markets'].update(markets)
            append_day(store, date_str, day)
            print(f"{date_str} 추가: {len(day)}종목")
        save_store(store)
        return 0
    if cmd == "insert":
        report_path, mode = sys.argv[2], sys.argv[3]
        with open(MARKET_DATA_PATH, encoding="utf-8") as f:
            result = json.load(f).get('breakout')
        if mode == "morning" and result:
            try:
                result = morning_refresh(result)
            except Exception as e:
                print(f"확정 수급 조회 실패: {e}")
        insert_after_title(report_path, section(mode, result))
        n = len(result['candidates']) if result else 0
        print(f"추세 돌파 섹션 추가: 후보 {n}개")
        return 0
    raise SystemExit(f"알 수 없는 명령: {cmd}")


if __name__ == "__main__":
    sys.exit(main())
