import yfinance as yf
import pandas as pd
import json
import datetime
import os
import sys
import time
import traceback
import re
import unicodedata
from snapshot_validation import kst_now, valid_cache, has_global_values

# 글로벌 전용 실행은 KRX 로그인과 무관하다. 임포트의 자동 로그인도 피한다.
if '--global-only' not in sys.argv:
    from pykrx import stock
    import FinanceDataReader as fdr
    from fetch_program import get_program_trading

def get_global_data(date_str):
    tickers = {
        'S&P500': '^GSPC',
        '다우': '^DJI',
        '나스닥': '^IXIC',
        'S&P500 선물': 'ES=F',
        '나스닥 선물': 'NQ=F',
        '다우 선물': 'YM=F',
        'SOX': '^SOX',
        '엔비디아': 'NVDA',
        '마이크로소프트': 'MSFT',
        '애플': 'AAPL',
        '미 10년물': '^TNX',
        'DXY': 'DX-Y.NYB',
        '원달러': 'KRW=X',
        'WTI': 'CL=F',
        '금': 'GC=F',
        'VIX': '^VIX'
    }

    results = {}
    end_date = pd.to_datetime(date_str) + pd.Timedelta(1, unit='D')
    start = (pd.to_datetime(date_str) - pd.Timedelta(days=10)).strftime('%Y-%m-%d')
    end = end_date.strftime('%Y-%m-%d')

    # Yahoo가 응답하는 엣지 서버/캐시에 따라 같은 티커·같은 날짜를 조회해도 종가가
    # NaN으로 비어있다가 몇 초 뒤 재요청하면 정상 값이 오는 경우가 실제로 확인됨
    # (일시적 오류가 아니라 요청마다 다른 캐시 상태에 걸리는 문제로 추정) — 몇 차례 재시도.
    for name, ticker in tickers.items():
        last_error = None
        for attempt in range(3):
            try:
                df = yf.download(ticker, start=start, end=end, progress=False)
                if df.empty:
                    last_error = '확인불가: 빈 데이터프레임'
                    continue
                current = df.iloc[-1]
                prev = df.iloc[-2] if len(df) > 1 else current
                close = float(current['Close'].iloc[0]) if isinstance(current['Close'], pd.Series) else float(current['Close'])
                prev_close = float(prev['Close'].iloc[0]) if isinstance(prev['Close'], pd.Series) else float(prev['Close'])
                if pd.isna(close) or pd.isna(prev_close):
                    last_error = '확인불가: 종가 데이터 미확정(Yahoo 반영 지연으로 추정)'
                    continue
                pct_change = (close - prev_close) / prev_close * 100
                results[name] = {
                    'close': round(close, 2),
                    'pct_change': round(pct_change, 2)
                }
                break
            except Exception as e:
                last_error = f'확인불가: {str(e)}'
            if attempt < 2:
                time.sleep(5)
        else:
            results[name] = last_error

    return results


def calculate_ichimoku(df):
    """
    df는 반드시 영문 컬럼명(Open, High, Low, Close, Volume)을 가지고 있어야 함.
    pykrx 데이터를 넣을 경우 호출 전에 컬럼명을 매핑할 것.
    """
    high_9 = df['High'].rolling(window=9).max()
    low_9 = df['Low'].rolling(window=9).min()
    tenkan_sen = (high_9 + low_9) / 2

    high_26 = df['High'].rolling(window=26).max()
    low_26 = df['Low'].rolling(window=26).min()
    kijun_sen = (high_26 + low_26) / 2

    # [수정] shift(25) -> shift(26): 표준 일목균형표는 선행스팬을 26봉 앞으로 이동
    senkou_span_a = ((tenkan_sen + kijun_sen) / 2).shift(26)

    high_52 = df['High'].rolling(window=52).max()
    low_52 = df['Low'].rolling(window=52).min()
    senkou_span_b = ((high_52 + low_52) / 2).shift(26)

    # [추가] 후행스팬(치코스팬): 현재 종가를 26봉 과거 자리에 표시해 추세 재확인용으로 사용
    chikou_span = df['Close'].shift(-26)

    df['tenkan'] = tenkan_sen
    df['kijun'] = kijun_sen
    df['span_a'] = senkou_span_a
    df['span_b'] = senkou_span_b
    df['chikou'] = chikou_span

    return df


# [수정] pykrx 한글 컬럼명 -> calculate_ichimoku가 기대하는 영문 컬럼명 매핑
ICHIMOKU_COLUMN_MAP = {
    '시가': 'Open',
    '고가': 'High',
    '저가': 'Low',
    '종가': 'Close',
    '거래량': 'Volume'
}


def _get_investor_net_buy(code, date_str):
    """단일 종목의 투자자별(외국인/기관/개인) 순매수 금액을 조회. 성공 시 dict, 실패 시 에러 문자열."""
    try:
        target_date = pd.to_datetime(date_str).strftime('%Y%m%d')
        df = stock.get_market_trading_value_by_investor(target_date, target_date, code)
        if df.empty:
            return '확인불가: 빈 데이터프레임'

        # 비거래일 등에는 pykrx가 빈 데이터프레임 대신 전 항목 0짜리 뼈대를 돌려줄 때가 있음(실제 버그로 확인됨).
        # '전체' 행의 매수/매도/순매수가 모두 0이면 거래 자체가 없었다는 뜻이라 확인불가로 처리한다.
        if '전체' in df.index:
            total = df.loc['전체']
            if total.get('매수', 0) == 0 and total.get('매도', 0) == 0 and total.get('순매수', 0) == 0:
                return '확인불가: 거래 없음(비거래일로 추정)'

        return {
            '외국인_순매수': int(df.loc['외국인', '순매수']) if '외국인' in df.index else None,
            '기관_순매수': int(df.loc['기관합계', '순매수']) if '기관합계' in df.index else None,
            '개인_순매수': int(df.loc['개인', '순매수']) if '개인' in df.index else None,
        }
    except Exception as e:
        return f'확인불가: {str(e)}'


SWING_FLOW_DAYS = 5


def _get_investor_5d(code, date_str):
    """최근 5거래일 투자자별 누적 순매수와 기관 5일 평균매수단가(기관 매수금액 합 ÷ 매수수량 합)."""
    try:
        end = pd.to_datetime(date_str)
        start = (end - pd.Timedelta(days=14)).strftime('%Y%m%d')
        end = end.strftime('%Y%m%d')
        net = stock.get_market_trading_value_by_date(start, end, code).tail(SWING_FLOW_DAYS)
        buy_value = stock.get_market_trading_value_by_date(start, end, code, on='매수').tail(SWING_FLOW_DAYS)
        buy_volume = stock.get_market_trading_volume_by_date(start, end, code, on='매수').tail(SWING_FLOW_DAYS)
        if net.empty or buy_value.empty or buy_volume.empty:
            return '확인불가: 빈 데이터프레임'
        def avg_buy_price(column):
            qty = int(buy_volume[column].sum())
            return round(int(buy_value[column].sum()) / qty) if qty > 0 else None

        return {
            '집계_거래일수': len(net),
            '일별_순매수': {d.strftime('%Y-%m-%d'): {'외국인': int(r['외국인합계']), '기관': int(r['기관합계'])}
                       for d, r in net.iterrows()},
            '외국인_5일누적순매수': int(net['외국인합계'].sum()),
            '기관_5일누적순매수': int(net['기관합계'].sum()),
            '개인_5일누적순매수': int(net['개인'].sum()),
            '기관_5일평균매수단가': avg_buy_price('기관합계'),
            '외국인_5일평균매수단가': avg_buy_price('외국인합계'),
        }
    except Exception as e:
        return f'확인불가: {str(e)}'


def _last_value(series):
    value = series.iloc[-1]
    return round(float(value), 2) if pd.notna(value) else None


def _next_round_figure(price):
    """현재가 바로 위의 심리적 마디가 (예: 596,000 → 600,000, 23,300 → 25,000)."""
    step = 5 * 10 ** max(len(str(int(price))) - 2, 0)
    return (int(price) // step + 1) * step


def _compute_swing(df, flow_5d, volume_scale=1.0):
    """스윙 타점 판정용 지표(이평선/RSI/MACD/거래량)와 기계적 판정 규칙 결과.
    volume_scale은 장중 실행 시 당일 누적 거래량을 하루치로 환산하는 배율."""
    close_s = df['Close'].astype(float)
    close = float(close_s.iloc[-1])
    open_ = float(df['Open'].iloc[-1])
    low = float(df['Low'].iloc[-1])
    pct_change = (close - close_s.iloc[-2]) / close_s.iloc[-2] * 100 if len(close_s) > 1 else 0.0

    ma = {n: _last_value(close_s.rolling(n).mean()) for n in (5, 10, 20, 60)}

    delta = close_s.diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = _last_value(100 - 100 / (1 + avg_gain / avg_loss))

    macd = close_s.ewm(span=12, adjust=False).mean() - close_s.ewm(span=26, adjust=False).mean()
    macd_osc = _last_value(macd - macd.ewm(span=9, adjust=False).mean())

    prev_vol_avg = df['Volume'].iloc[-6:-1].mean() if len(df) >= 6 else None
    volume_ratio = round(float(df['Volume'].iloc[-1]) * volume_scale / prev_vol_avg * 100, 1) if prev_vol_avg else None

    high_20d = int(df['High'].tail(20).max())
    high_60d = int(df['High'].tail(60).max())
    high_all = int(df['High'].max())

    ma5, ma20 = ma[5], ma[20]
    body_pct = (close - open_) / open_ * 100 if open_ else 0.0
    long_bearish = body_pct <= -3.0
    breakdown = bool(long_bearish and ma5 and ma20 and close < ma5 and close < ma20)
    # 갭하락처럼 몸통은 작아도 등락률이 큰 날까지 포함해 급락일은 눌림목으로 보지 않는다.
    sharp_drop = bool(pct_change <= -5.0 or body_pct <= -5.0)

    if ma20 is None:
        ma20_status = '확인불가(데이터 부족)'
    elif close < ma20:
        ma20_status = '20일선 아래(이탈)'
    elif (close - ma20) / ma20 * 100 <= 3.0 or low <= ma20 * 1.01:
        ma20_status = '20일선 위 지지 테스트'
    else:
        ma20_status = f'20일선 위(이격 +{(close - ma20) / ma20 * 100:.1f}%)'

    panic_volume = bool(pct_change < 0 and volume_ratio is not None and volume_ratio >= 150 and long_bearish)
    if panic_volume:
        volume_pattern = '대량 거래 동반 음봉(패닉셀/세력 이탈 위험)'
    elif pct_change < 0 and volume_ratio is not None and volume_ratio < 70:
        volume_pattern = '조정 시 거래량 급감(건전한 숨고르기)'
    else:
        volume_pattern = '특이 패턴 없음'

    # 손절선: 20일선 기준이되 현재가 대비 -2%~-3% 범위로 맞춘다.
    stop = target_1 = target_2 = rr = basis_1 = basis_2 = round_figure = stop_basis = None
    new_high = False
    if ma20 is not None and close >= ma20:
        if ma20 > close * 0.98:
            stop, stop_basis = int(round(close * 0.98)), '현재가 -2% (20일선이 너무 가까워 하향 조정)'
        elif ma20 < close * 0.97:
            stop, stop_basis = int(round(close * 0.97)), '현재가 -3% 마지노선 (20일선 이격이 커서 상향 조정)'
        else:
            stop, stop_basis = int(round(ma20)), '20일선 종가 이탈 라인'
        resistances = [(high_20d, '20일 고점'), (high_60d, '60일 고점'), (high_all, '4개월 고점'),
                       (ma20, '20일선'), (ma[60], '60일선')]
        merged = {}
        for price, label in resistances:
            if price is not None and price > close * 1.005:
                merged.setdefault(int(round(price)), []).append(label)
        levels = sorted(merged.items())
        if levels:
            target_1, basis_1 = levels[0][0], '·'.join(levels[0][1])
            nxt = next((lv for lv in levels[1:] if lv[0] > target_1 * 1.01), None)
            if nxt:
                target_2, basis_2 = nxt[0], '·'.join(nxt[1])
            rr = round((target_1 - close) / (close - stop), 2)
        else:
            new_high = True
            round_figure = _next_round_figure(close)

    if ma20 is None or rsi is None:
        verdict = '확인불가(데이터 부족)'
    elif close < ma20 or breakdown or panic_volume:
        verdict = '진입 금지(추세 이탈)'
    elif rsi >= 50 and ma20_status == '20일선 위 지지 테스트' and rr is not None and rr >= 2 and not sharp_drop:
        verdict = '매수 유력(종가 확인)'
    else:
        verdict = '관망(지지 테스트)'

    def safety_margin(investor):
        if not isinstance(flow_5d, dict) or not flow_5d.get(f'{investor}_5일평균매수단가'):
            return None
        avg = flow_5d[f'{investor}_5일평균매수단가']
        gap = (close - avg) / avg * 100
        buying = flow_5d[f'{investor}_5일누적순매수'] > 0
        # 5일간 순매도했다면 평단가 아래라는 사실은 지지 근거가 아니라 이탈 신호라 가산점을 주지 않는다.
        if not buying:
            status = f'해당 없음 ({investor} 5일 순매도 — 수급 이탈)'
        elif gap <= 2.0:
            status = '안전마진 확보'
        else:
            status = '안전마진 없음'
        return {'현재가_괴리율': round(gap, 2), f'{investor}_5일_순매수_주도': buying, '안전마진': status}

    inst_margin = safety_margin('기관')
    foreign_margin = safety_margin('외국인')

    # 매수 유력 종목이 여럿일 때 고르는 순위 기준(판정일의 전 거래일 기준).
    # 2024~2026 백테스트에서 두 조건을 모두 갖춘 신호가 두 기간 모두 성과가 나았지만 통계적으로 확실하진 않다.
    ma20_s = close_s.rolling(20).mean()
    daily = flow_5d.get('일별_순매수', {}) if isinstance(flow_5d, dict) else {}

    def pick_at(pos):
        if len(df) < -pos:
            return {'전일_양봉_20일선_지지': None, '전일_기관_또는_외국인_순매수': None, '점수': 0}
        p_open, p_close, p_low, p_ma20 = (float(df['Open'].iloc[pos]), float(close_s.iloc[pos]),
                                          float(df['Low'].iloc[pos]), ma20_s.iloc[pos])
        bull = bool(p_close > p_open and p_close >= p_ma20 and p_low <= p_ma20 * 1.03) if pd.notna(p_ma20) else None
        day = daily.get(df.index[pos].strftime('%Y-%m-%d'))
        flow = (day['기관'] > 0 or day['외국인'] > 0) if day else None
        return {'전일_양봉_20일선_지지': bull, '전일_기관_또는_외국인_순매수': flow, '점수': int(bool(bull)) + int(bool(flow))}

    # pick: 판정일의 전 거래일 기준(당일 종가배팅용). pick_next: 판정일 당일 기준(다음 거래일 진입용 — 그날 보면 '전일').
    pick, pick_next = pick_at(-2), pick_at(-1)

    return {
        'ma5': ma[5], 'ma10': ma[10], 'ma20': ma20, 'ma60': ma[60],
        'rsi14': rsi,
        'macd_osc': macd_osc,
        'volume_ratio_5d_pct': volume_ratio,
        'candle_body_pct': round(body_pct, 2),
        'high_20d': high_20d, 'high_60d': high_60d, 'high_lookback': high_all,
        'flow_5d': flow_5d,
        'rules': {
            'rsi_trend': None if rsi is None else ('상승 추세 우위' if rsi >= 50 else '하락 압력 우위(원칙적 매수 보류)'),
            'ma20_status': ma20_status,
            'long_bearish_breakdown': breakdown,
            'sharp_drop_day': sharp_drop,
            'volume_pattern': volume_pattern,
            'inst_margin': inst_margin,
            'foreign_margin': foreign_margin,
        },
        'levels': {
            'stop': stop,
            'stop_basis': stop_basis,
            'target_1': target_1,
            'target_1_basis': basis_1,
            'target_2': target_2,
            'target_2_basis': basis_2,
            'risk_reward': rr,
            'new_high': new_high,
            'round_figure': round_figure,
        },
        'pick': pick,
        'pick_next': pick_next,
        'verdict': verdict,
    }


def _compute_ichimoku_snapshot(code, date_str, with_swing=False, volume_scale=1.0):
    """
    단일 종목의 일목균형표 스냅샷(종가/등락률/전환선/기준선/구름대)을 계산.
    with_swing=True면 스윙 타점 판정용 지표(swing)도 함께 계산한다.
    성공 시 dict, 실패 시 '확인불가: ...' 문자열을 반환.
    """
    try:
        start_date = (pd.to_datetime(date_str) - pd.Timedelta(days=120)).strftime('%Y%m%d')
        target_date = pd.to_datetime(date_str).strftime('%Y%m%d')

        df = stock.get_market_ohlcv(start_date, target_date, code)

        if df.empty:
            return '확인불가: pykrx 반환 데이터 없음'

        # [핵심 수정] 컬럼명 매핑 — 이게 빠져 있어서 지금까지 5종목이 전부 확인불가였음
        df = df.rename(columns=ICHIMOKU_COLUMN_MAP)

        # 필요한 컬럼이 실제로 존재하는지 방어적으로 체크
        required_cols = {'Open', 'High', 'Low', 'Close', 'Volume'}
        if not required_cols.issubset(df.columns):
            missing = required_cols - set(df.columns)
            return f'확인불가: 컬럼 누락 {missing}'

        df = calculate_ichimoku(df)

        today_data = df.iloc[-1]
        prev_data = df.iloc[-2] if len(df) > 1 else today_data

        close = int(today_data['Close'])
        prev_close = int(prev_data['Close'])
        pct_change = (close - prev_close) / prev_close * 100

        span_a = float(today_data['span_a'])
        span_b = float(today_data['span_b'])
        upper_cloud = max(span_a, span_b)
        lower_cloud = min(span_a, span_b)

        if close > upper_cloud:
            position = '구름대 위'
        elif close < lower_cloud:
            position = '구름대 아래'
        else:
            position = '구름대 안'

        # 후행스팬 참고용 값 (26봉 뒤 자리라 최근 구간은 NaN일 수 있음)
        chikou = today_data.get('chikou', None)
        chikou_val = round(float(chikou), 2) if pd.notna(chikou) else None

        snapshot = {
            'close': close,
            'pct_change': round(pct_change, 2),
            'open': int(today_data['Open']),
            'high': int(today_data['High']),
            'low': int(today_data['Low']),
            'volume': int(today_data['Volume']),
            'tenkan': round(float(today_data['tenkan']), 2),
            'kijun': round(float(today_data['kijun']), 2),
            'cloud_upper': round(upper_cloud, 2),
            'cloud_lower': round(lower_cloud, 2),
            'cloud_pos': position,
            'chikou': chikou_val,
            'recent_high_120d': int(df['High'].max()),
            'investor': _get_investor_net_buy(code, date_str)
        }
        if with_swing:
            try:
                snapshot['swing'] = _compute_swing(df, _get_investor_5d(code, date_str), volume_scale)
            except Exception as e:
                snapshot['swing'] = f'확인불가: {str(e)}'
        return snapshot
    except Exception as e:
        return f'확인불가: {str(e)}'


# 개인 설정(portfolio_config.md)은 git에서 제외한다. 없으면 공개용 예시 파일을 읽는다.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(_REPO_ROOT, "portfolio_config.md")
if not os.path.exists(TEMPLATE_PATH):
    TEMPLATE_PATH = os.path.join(_REPO_ROOT, "portfolio_config.example.md")


def _load_fixed_watchlist_names():
    """템플릿의 '### 관심종목 (고정)' 섹션에서 종목명 리스트를 읽어온다."""
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    match = re.search(r"### 관심종목 \(고정\)\s*\n((?:- .+\n?)+)", content)
    if not match:
        return []

    lines = match.group(1).strip().split("\n")
    return [line[1:].strip() for line in lines if line.strip().startswith("-")]


def _build_name_to_code_map(date_str):
    """pykrx 전체 종목 목록에서 종목명 -> (종목코드, 시장) 매핑을 만든다.
    코스피/코스닥을 나눠 조회해야 어느 시장 소속인지 알 수 있다 — market="ALL"로는
    구분이 안 돼, 고정 관심종목 리포트가 매번 "KOSPI/KOSDAQ 여부: 확인되지 않음"으로
    나오던 문제가 있었다(2026-09-07, 시장별 손절 기준 도입 후 발견)."""
    target_date = pd.to_datetime(date_str).strftime('%Y%m%d')
    mapping = {}
    for market in ("KOSPI", "KOSDAQ"):
        for code in stock.get_market_ticker_list(target_date, market=market):
            mapping[stock.get_market_ticker_name(code)] = (code, market)
    return mapping


def _normalize_stock_name(name):
    """영문 대소문자·전각 문자 차이를 무시하고 종목명을 비교한다."""
    return unicodedata.normalize("NFKC", name).casefold()


def get_individual_stocks(date_str, volume_scale=1.0):
    names = _load_fixed_watchlist_names()
    if not names:
        return {}

    try:
        name_to_code = _build_name_to_code_map(date_str)
    except Exception as e:
        return {name: f'확인불가: 종목코드 매핑 조회 실패 - {e}' for name in names}

    normalized_name_to_entry = {
        _normalize_stock_name(canonical_name): (canonical_name, code, market)
        for canonical_name, (code, market) in name_to_code.items()
    }

    results = {}
    for requested_name in names:
        canonical_name = requested_name
        code, market = name_to_code.get(requested_name, (None, None))
        if not code:
            match = normalized_name_to_entry.get(_normalize_stock_name(requested_name))
            if match:
                canonical_name, code, market = match
        if not code:
            results[requested_name] = '확인불가: 종목코드 매칭 실패'
            continue
        snapshot = _compute_ichimoku_snapshot(code, date_str, with_swing=True, volume_scale=volume_scale)
        if isinstance(snapshot, dict):
            snapshot['market'] = market
            snapshot['code'] = code
        results[canonical_name] = snapshot

    return results


def _get_market_investor_flow(target_date, market):
    """코스피/코스닥 각각의 투자자별(외국인/기관/개인) 순매수를 조회.
    이전에는 코스피만 조회해서 '수급 현황'이라는 이름과 다르게 코스닥이 빠져있었음(실제 문제 확인)."""
    try:
        investor_df = stock.get_market_trading_value_by_investor(target_date, target_date, market)
        if investor_df.empty:
            return '확인불가: 빈 데이터프레임'
        result = {}
        for idx in investor_df.index:
            if '개인' in idx or '외국인' in idx or '기관합계' in idx:
                result[idx] = int(investor_df.loc[idx, '순매수'])
        return result
    except Exception as e:
        return f'확인불가: {str(e)}'


def _get_index_snapshot(target_date, ticker):
    """코스피/코스닥 종합지수 자체의 종가·등락률(전일 대비)을 조회.
    이전에는 지수 값 자체가 파이프라인에 없어 리포트가 매번 "확인되지 않음"으로
    표시했음(실제 문제 확인, 2026-09-09)."""
    try:
        start_date = (pd.to_datetime(target_date) - pd.Timedelta(days=10)).strftime('%Y%m%d')
        df = stock.get_index_ohlcv(start_date, target_date, ticker)
        if df.empty:
            return '확인불가: pykrx 반환 데이터 없음'
        today_row = df.iloc[-1]
        prev_close = float(df.iloc[-2]['종가']) if len(df) > 1 else float(today_row['종가'])
        close = float(today_row['종가'])
        pct_change = (close - prev_close) / prev_close * 100 if prev_close else None
        return {
            '종가': round(close, 2),
            '등락률': round(pct_change, 2) if pct_change is not None else None,
            '시가': round(float(today_row['시가']), 2),
            '고가': round(float(today_row['고가']), 2),
            '저가': round(float(today_row['저가']), 2),
            '거래대금': int(today_row['거래대금']),
        }
    except Exception as e:
        return f'확인불가: {str(e)}'


def get_index_snapshot(date_str):
    target_date = pd.to_datetime(date_str).strftime('%Y%m%d')
    return {
        '코스피': _get_index_snapshot(target_date, '1001'),
        '코스닥': _get_index_snapshot(target_date, '2001'),
    }


def get_market_flow(date_str):
    target_date = pd.to_datetime(date_str).strftime('%Y%m%d')
    results = {
        '코스피': _get_market_investor_flow(target_date, 'KOSPI'),
        '코스닥': _get_market_investor_flow(target_date, 'KOSDAQ'),
    }

    # pykrx에는 프로그램매매 조회 함수가 없어 네이버 금융 크롤링(fetch_program.py)으로 대체
    # (프로그램매매는 코스피 전용 통계라 코스닥 항목이 따로 없음)
    results.update(get_program_trading(date_str))

    return results


def get_candidates(date_str):
    target_date = pd.to_datetime(date_str).strftime('%Y%m%d')
    results = {}

    # KRX 서버가 가끔 연결을 끊어버리는 일시적 오류(ConnectionResetError)가 실제로
    # 발생함 — 재시도 없이 바로 실패 처리하던 걸 몇 차례 재시도하도록 변경.
    last_error = None
    for attempt in range(3):
        try:
            df_kospi = stock.get_market_ohlcv(target_date, market="KOSPI")
            df_kospi['시장'] = 'KOSPI'
            df_kosdaq = stock.get_market_ohlcv(target_date, market="KOSDAQ")
            df_kosdaq['시장'] = 'KOSDAQ'

            df_all = pd.concat([df_kospi, df_kosdaq])

            # 비거래일 등에는 pykrx가 전종목 거래대금 0짜리 뼈대를 돌려줄 때가 있음(실제 버그로 확인됨)
            if df_all.empty or df_all['거래대금'].sum() == 0:
                raise ValueError('전종목 거래대금 합계 0 — 비거래일로 추정')

            df_all = df_all.sort_values('거래대금', ascending=False).head(20)

            candidates = []
            for code, row in df_all.iterrows():
                name = stock.get_market_ticker_name(code)
                candidates.append({
                    'name': name,
                    'code': code,
                    'market': row['시장'],
                    'value': int(row['거래대금']),
                    'pct_change': round(float(row['등락률']), 2)
                })
            results['top_value'] = candidates
            break
        except Exception as e:
            last_error = f'확인불가: {str(e)}'
            if attempt < 2:
                time.sleep(5)
    else:
        results['top_value'] = last_error

    last_error = None
    for attempt in range(3):
        try:
            inv_kospi = stock.get_market_net_purchases_of_equities_by_ticker(target_date, target_date, "KOSPI", "외국인")
            inv_kospi['시장'] = 'KOSPI'
            inv_kosdaq = stock.get_market_net_purchases_of_equities_by_ticker(target_date, target_date, "KOSDAQ", "외국인")
            inv_kosdaq['시장'] = 'KOSDAQ'
            inv_all = pd.concat([inv_kospi, inv_kosdaq])
            inv_all = inv_all.sort_values('순매수거래대금', ascending=False).head(10)

            fr_candidates = []
            for code, row in inv_all.iterrows():
                fr_candidates.append({
                    'name': row['종목명'],
                    'code': code,
                    'market': row['시장'],
                    'net_buy': int(row['순매수거래대금'])
                })
            results['top_foreign'] = fr_candidates
            break
        except Exception as e:
            last_error = f'확인불가: {str(e)}'
            if attempt < 2:
                time.sleep(5)
    else:
        results['top_foreign'] = last_error

    return results


TRACKING_PATH = "watchlist_tracking.json"
TRACKING_WINDOW_DAYS = 7  # 거래일 기준 약 5일 + 주말 여유


def _load_tracking():
    try:
        with open(TRACKING_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_tracking(entries):
    with open(TRACKING_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def get_watchlist_candidates(date_str, candidates_data):
    """
    거래대금 상위 20(top_value) ∩ 외국인 순매수 상위 10(top_foreign) 종목에 대해
    고정 5종목과 동일한 방식으로 일목균형표를 계산.
    candidates_data는 get_candidates()의 반환값을 그대로 받는다.

    한 번 잡힌 종목은 TRACKING_WINDOW_DAYS일 동안 watchlist_tracking.json에 남아서,
    오늘 순위(거래대금/외국인 상위)에서 빠지더라도 눌림목 전환 여부를 계속 관찰한다.
    """
    top_value = candidates_data.get('top_value')
    top_foreign = candidates_data.get('top_foreign')

    tracking = _load_tracking()
    cutoff = pd.to_datetime(date_str) - pd.Timedelta(days=TRACKING_WINDOW_DAYS)
    tracking = [t for t in tracking if pd.to_datetime(t['first_seen']) >= cutoff]
    tracked_codes = {t['code'] for t in tracking}

    fresh_reason_by_code = {}
    fresh_status_by_code = {}
    # top_value/top_foreign 조회 자체가 실패한 날(문자열 에러)이 아닐 때만 오늘 신규 교집합 계산
    if isinstance(top_value, list) and isinstance(top_foreign, list):
        value_by_code = {c['code']: c for c in top_value if c.get('code')}
        foreign_by_code = {c['code']: c for c in top_foreign if c.get('code')}
        overlap_codes = [code for code in value_by_code if code in foreign_by_code]

        for code in overlap_codes:
            v_entry = value_by_code[code]
            f_entry = foreign_by_code[code]
            name = v_entry.get('name') or f_entry.get('name')
            market = v_entry.get('market') or f_entry.get('market')
            value_rank = top_value.index(v_entry) + 1
            foreign_rank = top_foreign.index(f_entry) + 1
            reason = f'거래대금 {value_rank}위 + 외국인 순매수 {foreign_rank}위'
            fresh_reason_by_code[code] = reason

            if code not in tracked_codes:
                tracking.append({'code': code, 'name': name, 'market': market, 'first_seen': date_str, 'reason': reason})
                tracked_codes.add(code)
                fresh_status_by_code[code] = '오늘 신규 발견'
            else:
                fresh_status_by_code[code] = '오늘도/다시 조건 충족'

    _save_tracking(tracking)

    results = []
    for t in tracking:
        code = t['code']
        name = t['name']
        reason = fresh_reason_by_code.get(code, t['reason'])
        days_tracked = (pd.to_datetime(date_str) - pd.to_datetime(t['first_seen'])).days

        status = fresh_status_by_code.get(code, f'{days_tracked}일째 관찰 중 (오늘 순위 밖)')

        entry = {
            'name': name,
            'code': code,
            'market': t.get('market'),
            'overlap_reason': reason,
            'tracking_status': status
        }

        snapshot = _compute_ichimoku_snapshot(code, date_str, with_swing=True)
        if isinstance(snapshot, dict):
            entry.update(snapshot)
            investor = entry.get('investor')
            foreign_net = investor.get('외국인_순매수') if isinstance(investor, dict) else None
            if foreign_net is not None and entry.get('pct_change', 0) < 0 and foreign_net > 0:
                entry['flag'] = '⚠️ 방향 불일치'
                
            # 눌림목 판단 (Actionability)
            is_pullback = False
            if entry.get('cloud_pos') == '구름대 위':
                pct = entry.get('pct_change', 0)
                if -3.5 <= pct < 0.0:
                    if entry.get('close', 0) >= entry.get('tenkan', 0):
                        is_pullback = True
            entry['is_pullback'] = is_pullback
        else:
            entry['status'] = snapshot
            entry['is_pullback'] = False

        results.append(entry)

    def sort_key(e):
        # 1. Freshness
        st = e.get('tracking_status', '')
        if st == '오늘 신규 발견':
            f_score = 0
        elif st == '오늘도/다시 조건 충족':
            f_score = 1
        else:
            f_score = 2
            
        # 2. Actionability
        a_score = 0 if e.get('is_pullback', False) else 1
        
        # 3. Trend
        cloud = e.get('cloud_pos', '')
        if cloud == '구름대 위':
            t_score = 0
        elif cloud == '구름대 안':
            t_score = 1
        else:
            t_score = 2
            
        # 4. Strength
        investor = e.get('investor')
        foreign_net = 0
        if isinstance(investor, dict):
            val = investor.get('외국인_순매수')
            if val is not None:
                foreign_net = val
                
        pct = e.get('pct_change', 0)
        
        return (f_score, a_score, t_score, -foreign_net, -pct)

    results.sort(key=sort_key)

    return results


def _last_trading_date():
    """
    장 마감(15:30 KST) 이후면 오늘, 그 전이면 직전 거래일을 반환.
    주말뿐 아니라 한국 공휴일(대체공휴일 포함)도 pykrx의 실제 거래일 캘린더로 판단한다.
    (주말만 계산하던 예전 로직이 8/17 광복절 대체공휴일을 거래일로 착각해 데이터가 깨진 적이 있음)
    """
    now = kst_now()
    is_weekday = now.weekday() < 5
    after_close = now.time() >= datetime.time(15, 30)
    if is_weekday and after_close:
        candidate = now.date()
    else:
        candidate = now.date() - datetime.timedelta(days=1)

    nearest = stock.get_nearest_business_day_in_a_week(date=candidate.strftime('%Y%m%d'), prev=True)
    return f"{nearest[:4]}-{nearest[4:6]}-{nearest[6:]}"


GLOBAL_DATA_CACHE_PATH = "global_data.json"


def _write_global_cache(date_str, global_data):
    with open(GLOBAL_DATA_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "date": date_str,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "global": global_data,
        }, f, ensure_ascii=False, indent=2)


def _load_global_cache(date_str):
    """오늘(date_str) 07:20에 이미 받아둔 글로벌 지수가 있으면 재사용해 야후 조회를
    하루 두 번 반복하지 않는다. 없거나 날짜가 다르면(예: 첫 실행, 07:20 실행 실패) None."""
    try:
        with open(GLOBAL_DATA_CACHE_PATH, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not valid_cache(cached, date_str):
        return None
    return cached


if __name__ == "__main__":
    if "--global-only" in sys.argv:
        # 07:20(장 시작 전) 실행 — 미국 지수/선물만 받아 캐시에 남긴다. KR 데이터는
        # 아직 손대지 않고, 16:00 본 실행이 이 캐시를 재사용한다.
        today = kst_now().date().isoformat()
        global_data = get_global_data(today)
        if not has_global_values(global_data):
            print('글로벌 지수 조회 실패 — 기존 캐시를 유지하고 발행 중단')
            sys.exit(1)
        _write_global_cache(today, global_data)
        print(f"Global data saved to {GLOBAL_DATA_CACHE_PATH} ({today})")
        sys.exit(0)

    date = _last_trading_date()
    if date != kst_now().date().isoformat() and '--force' not in sys.argv:
        print('오늘 KR 정규장 마감 데이터 없음 — 휴장일/장 마감 전 실행 생략 (--force로 마지막 거래일 강제 재생성 가능)')
        sys.exit(3)
    if date != kst_now().date().isoformat():
        print(f'--force: 오늘이 아닌 마지막 거래일({date}) 데이터로 재생성')
    candidates_data = get_candidates(date)
    watchlist = get_watchlist_candidates(date, candidates_data)
    cached_global = _load_global_cache(date)
    if cached_global:
        global_data = cached_global["global"]
        global_fetched_at = cached_global["fetched_at"]
    else:
        global_data = get_global_data(date)
        global_fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    final_data = {
        "global": global_data,
        "global_fetched_at": global_fetched_at,
        "global_snapshot_date": date,
        "stocks": get_individual_stocks(date),
        "index": get_index_snapshot(date),
        "flow": get_market_flow(date),
        "candidates": candidates_data,
        "watchlist_candidates": watchlist,
        # 역추세 반등 관찰은 리포트에서 뺐다(2026-10-21). 종목별 조회가 매일 100회 넘게 들어 KRX 접속 제한 위험만 키우므로
        # 스캔하지 않고 빈 목록으로 둔다(수동 점검용 publish_public_snapshot.py가 키 존재를 기대함).
        "technical_rebound_candidates": []
    }
    # 주 신호(추세 돌파). 시장 전체 하루치만 4회 조회해 로컬 저장소에 쌓는다 — 종목별 대량 조회로 KRX 접속이 막힌 적이 있다(2026-10-20).
    try:
        import breakout_signal
        final_data["breakout"] = breakout_signal.update_and_signal(date)
        print(f"추세 돌파 후보 {len(final_data['breakout']['candidates'])}개, 매도 신호 {len(final_data['breakout']['sells'])}개")
    except Exception as e:
        print(f"추세 돌파 계산 실패: {e}")
        final_data["breakout"] = None
    with open("market_data.json", "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=2)
    print("Data saved to market_data.json")

    print(f"Watchlist candidates ({date}): {len(watchlist)}건")
    for item in watchlist:
        if 'status' in item:
            print(f"  - {item['name']}({item['code']}): {item['status']}")
        else:
            flag = f" {item['flag']}" if 'flag' in item else ""
            print(f"  - {item['name']}({item['code']}): 종가 {item['close']:,} ({item['pct_change']:+.2f}%), "
                  f"{item['cloud_pos']}, {item['overlap_reason']}{flag}")
