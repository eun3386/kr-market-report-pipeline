"""장 판단 기준 백테스트: 추세 돌파 신호를 장 판단(좋음/엇갈림/나쁨)별, 신호 날 상승률별로 나눠 성과를 비교한다.

신호·매도는 breakout_signal.py와 같다: 구름대 위 + 직전 20거래일 최고가 돌파 → 그날 종가 매수,
종가가 이틀 연속 10일선 아래면 둘째 날 종가 매도(최대 20거래일), 같은 종목은 들고 있는 동안 새 신호 무시, 비용 0.25%.
장 판단도 같다: 코스피 > 60일선, 코스피 > 20일선, 대상 종목 중 20일선 위 비율 ≥ 50% — 셋 다면 좋음, 셋 다 아니면 나쁨.

데이터: 로컬 저장소/캐시만 읽고, 네트워크 조회는 코스피 지수 일봉 1회(pykrx, 실패하면 yfinance ^KS11)뿐이다.
  python3 backtest_regime.py                      # breakout_history.pkl
  python3 backtest_regime.py kr_cache.pkl old.pkl  # 백테스트용 캐시 여러 개 ({code: (market, df)} 또는 저장소 형식)
결과는 화면에 출력하고 backtest_regime_result.md에도 저장한다.
"""
import pickle
import sys

import pandas as pd

import breakout_signal as bs

OUT_PATH = f"{bs.BASE}/backtest_regime_result.md"


def load_frames(paths):
    """여러 파일을 합쳐 필드별 (날짜 × 종목) 표로 만든다. 먼저 준 파일이 우선."""
    frames = {f: pd.DataFrame() for f in ('Open', 'High', 'Low', 'Close', 'Volume', 'foreign')}
    for path in paths:
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if isinstance(obj, dict) and isinstance(obj.get('Close'), pd.DataFrame):
            part = {k: obj[k] for k in frames if k in obj}
        else:
            part = {k: pd.DataFrame({code: df[k] for code, (_, df) in obj.items() if k in df}) for k in frames}
        for k, v in part.items():
            if len(v):
                v = v.copy()
                v.index = pd.to_datetime(v.index)
                frames[k] = frames[k].combine_first(v) if len(frames[k]) else v
    idx = frames['Close'].index.sort_values()
    cols = frames['Close'].columns
    return {k: v.reindex(index=idx, columns=cols).astype(float) for k, v in frames.items()}


def load_kospi(start, end):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    s, e = (start - pd.Timedelta(days=150)).strftime('%Y%m%d'), end.strftime('%Y%m%d')
    try:
        from pykrx import stock
        k = stock.get_index_ohlcv(s, e, '1001')['종가'].astype(float)
        if len(k):
            k.index = pd.to_datetime(k.index)
            return k
    except Exception as ex:
        print(f"pykrx 코스피 조회 실패, yfinance로 재시도: {ex}")
    import yfinance as yf
    k = yf.download('^KS11', start=start - pd.Timedelta(days=150), end=end + pd.Timedelta(days=1), progress=False)['Close']
    k = k.iloc[:, 0] if isinstance(k, pd.DataFrame) else k
    k.index = pd.to_datetime(k.index).tz_localize(None)
    return k.astype(float)


def signals(fr):
    C, H, L = fr['Close'], fr['High'], fr['Low']
    tenkan = (H.rolling(9).max() + L.rolling(9).min()) / 2
    kijun = (H.rolling(26).max() + L.rolling(26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((H.rolling(52).max() + L.rolling(52).min()) / 2).shift(26)
    cloud_top = span_a.where(span_a > span_b, span_b)
    enough = C.notna().cumsum() >= bs.MIN_HISTORY
    return enough & (C > cloud_top) & (C > H.shift(1).rolling(20).max())


def tiers(fr, lag=0):
    """등급 (breakout_signal.compute와 같은 규칙). lag=0: 당일 수급까지(16:00), lag=1: 전 거래일까지(15:10 예비 등급).
    외국인 데이터가 없으면 None. 저장소 과거 수급은 확정치라, 16:00 잠정치로 매기는 실제 등급과 조금 다를 수 있다."""
    F, C, V = fr['foreign'].shift(lag), fr['Close'], fr['Volume']
    f_rank = F.rank(axis=1, ascending=False)
    v_rank = (C * V).shift(lag).rank(axis=1, ascending=False)
    flows = F.rolling(5).sum()
    flows_ok = F.notna().rolling(5).sum() == 5
    t = pd.DataFrame(3.0, index=C.index, columns=C.columns)
    t[flows_ok & (flows > 0)] = 2
    t[(f_rank <= 10) & (v_rank <= 20)] = 1
    t.loc[F.isna().all(axis=1)] = float('nan')  # 그날 외국인 데이터가 아예 없음
    return t


def regime(fr, kospi):
    C = fr['Close']
    ma20 = C.rolling(20).mean()
    breadth = ((C > ma20).where(ma20.notna())).mean(axis=1) * 100
    k = kospi.reindex(C.index.union(kospi.index)).ffill()
    df = pd.DataFrame({'k60': k > k.rolling(60).mean(), 'k20': k > k.rolling(20).mean()}).reindex(C.index)
    df['k60'] = df['k60'].where(k.rolling(60).mean().reindex(C.index).notna())
    df['k20'] = df['k20'].where(k.rolling(20).mean().reindex(C.index).notna())
    df['breadth'] = (breadth >= 50).where(breadth.notna())
    n = df[['k60', 'k20', 'breadth']].sum(axis=1)
    ok = df[['k60', 'k20', 'breadth']].notna().all(axis=1)
    df['verdict'] = pd.Series('엇갈림', index=C.index).where(~(n == 3), '좋음').where(~(n == 0), '나쁨').where(ok)
    return df


def rsi_macd(C):
    """RSI(14)와 MACD 오실레이터 — fetch_data._compute_swing과 같은 계산식."""
    delta = C.diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - 100 / (1 + avg_gain / avg_loss)
    macd = C.ewm(span=12, adjust=False).mean() - C.ewm(span=26, adjust=False).mean()
    return rsi, macd - macd.ewm(span=9, adjust=False).mean()


def trades(fr, sig, tier, reg):
    C = fr['Close']
    ma10 = C.rolling(10).mean()
    chg = (C / C.shift(1) - 1) * 100
    body = (C / fr['Open'] - 1) * 100  # 시가 대비 종가: 플러스면 장 초반 이후에도 버티거나 더 오름, 마이너스면 밀림
    over = (C / fr['High'].shift(1).rolling(20).max() - 1) * 100  # 종가가 직전 20일 최고가를 넘은 폭
    rsi, osc = rsi_macd(C)
    base = C.rolling(9).mean()  # 다음 날 기준가(직전 9거래일 종가 평균) — 이보다 낮게 마감하면 10일선 아래
    pos = {d: i for i, d in enumerate(C.index)}
    out = []
    for code in C.columns:
        days = sig.index[sig[code].fillna(False).values]
        c, m = C[code], ma10[code]
        held_until = None
        for d in days:
            if held_until is not None and d <= held_until:
                continue
            i0, below, exit_i, n = pos[d], 0, None, 0
            for i in range(i0 + 1, len(C.index)):
                if pd.isna(c.iat[i]):
                    continue
                n += 1
                below = below + 1 if c.iat[i] < m.iat[i] else 0
                if below >= bs.SELL_CONFIRM_DAYS or n >= bs.SELL_LOOKBACK:
                    exit_i = i
                    break
            if exit_i is None:
                break  # 아직 안 끝난 거래는 뺀다
            r = reg.loc[d]
            out.append({'date': d, 'code': code, 'tier': tier.at[d, code], 'day_pct': chg.at[d, code],
                        'cushion': (c.iat[i0] / base.at[d, code] - 1) * 100,
                        'body': body.at[d, code], 'over': over.at[d, code], 'rsi': rsi.at[d, code], 'osc': osc.at[d, code],
                        'pnl': (c.iat[exit_i] / c.iat[i0] - 1) * 100 - bs.COST_PCT,
                        'verdict': r['verdict'], 'k60': r['k60'], 'k20': r['k20'], 'breadth': r['breadth']})
            held_until = C.index[exit_i]
    return pd.DataFrame(out)


EXIT_RULES = (('지금 규칙: 10일선 이틀 연속', None, 10), ('여유 15% 이상만 5일선 이틀 연속', 15, 5),
              ('여유 10% 이상만 5일선 이틀 연속', 10, 5), ('전부 5일선 이틀 연속', 0, 5))


def exit_trades(fr, sig, cushion_min, fast_ma):
    """매도 규칙만 바꿔 다시 돌린다: 신호 날 기준가까지 여유가 cushion_min% 이상인 거래는 fast_ma일선 이틀 연속 이탈로 판다."""
    C = fr['Close']
    base = C.rolling(9).mean()
    ma = {10: C.rolling(10).mean(), fast_ma: C.rolling(fast_ma).mean()}
    out = []
    for code in C.columns:
        c = C[code].values
        held = -1
        for i0 in sig.index.get_indexer(sig.index[sig[code].fillna(False).values]):
            if i0 <= held:
                continue
            cushion = (c[i0] / base[code].iat[i0] - 1) * 100
            m = ma[fast_ma if cushion_min is not None and cushion >= cushion_min else 10][code].values
            below, n, exit_i = 0, 0, None
            for i in range(i0 + 1, len(C.index)):
                if pd.isna(c[i]):
                    continue
                n += 1
                below = below + 1 if c[i] < m[i] else 0
                if below >= bs.SELL_CONFIRM_DAYS or n >= bs.SELL_LOOKBACK:
                    exit_i = i
                    break
            if exit_i is None:
                break
            out.append({'date': C.index[i0], 'code': code, 'exit': C.index[exit_i], 'cushion': cushion, 'days': n, 'pnl': (c[exit_i] / c[i0] - 1) * 100 - bs.COST_PCT})
            held = exit_i
    return pd.DataFrame(out)


def exit_stats(t):
    lose = t.loc[t['pnl'] <= 0, 'pnl']
    return {'거래 수': len(t), '거래당 평균%': round(t['pnl'].mean(), 2), '중앙값%': round(t['pnl'].median(), 2),
            '이긴 비율%': round((t['pnl'] > 0).mean() * 100), '질 때 평균%': round(lose.mean(), 2),
            '나쁜 쪽 10%': round(t['pnl'].quantile(0.1), 1), '최악%': round(t['pnl'].min(), 1), '평균 보유일': round(t['days'].mean(), 1)}


def summarize(t, by):
    g = t.groupby(by, dropna=False, observed=False)['pnl']
    return pd.DataFrame({'거래 수': g.size(), '거래당 평균%': g.mean().round(2), '중앙값%': g.median().round(2), '이긴 비율%': (g.apply(lambda s: (s > 0).mean()) * 100).round(0),
                         '합계%': g.sum().round(0)})


def main():
    paths = sys.argv[1:] or [bs.STORE_PATH]
    fr = load_frames(paths)
    C = fr['Close']
    print(f"데이터: {len(C.columns)}종목, {C.index.min().date()}~{C.index.max().date()} ({len(C)}거래일)")
    kospi = load_kospi(C.index.min(), C.index.max())
    sig = signals(fr)
    t = trades(fr, sig, tiers(fr), regime(fr, kospi))
    t = t[t['verdict'].notna()]
    t['연도'] = t['date'].dt.year
    t['구간'] = t['date'].map(lambda d: f"{d.year if d.month >= 10 else d.year - 1}-10~" if d >= pd.Timestamp('2024-10-01') else "2024-09 이전")
    t['등급'] = t['tier'].map(lambda x: '외국인 데이터 없음' if pd.isna(x) else f"{int(x)}등급")
    order = ['좋음', '엇갈림', '나쁨']

    def md(df):
        return df.to_markdown() if hasattr(df, 'to_markdown') and _has_tabulate() else df.to_string()

    parts = [f"# 장 판단 기준 백테스트 ({pd.Timestamp.now():%Y-%m-%d})", "",
             f"- 데이터: {', '.join(paths)} — {len(C.columns)}종목, {C.index.min().date()}~{C.index.max().date()}",
             f"- 끝난 거래 {len(t)}건 (장 판단을 매길 수 있는 날만, 비용 {bs.COST_PCT}% 뺀 값)",
             "- 주의: 지금 저장소에 남아 있는 종목만으로 돌렸다(그 사이 상장폐지·시총 탈락 종목이 빠져 성과가 좋게 나올 수 있음).",
             "- 주의: 아직 매도 조건에 안 걸린 거래는 뺐다 — 최근 구간(특히 마지막 한두 달)은 빨리 끝난 손실 거래만 남아 실제보다 나쁘게 보인다.", "",
             "## 1. 장 판단별 전체", "", md(summarize(t, 'verdict').reindex(order)), "",
             "## 2. 장 판단별 × 연도 (거래당 평균%, 괄호는 거래 수)", "",
             md(t.pivot_table(index='연도', columns='verdict', values='pnl', aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})").reindex(columns=order)), "",
             "## 3. 장 판단별 × 구간 (2024-10 이후는 기존 백테스트 구간과 같은 10월~9월)", "",
             md(t.pivot_table(index='구간', columns='verdict', values='pnl', aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})").reindex(columns=order)), "",
             "## 4. 장 판단별 × 등급", "",
             md(t.pivot_table(index='등급', columns='verdict', values='pnl', aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})").reindex(columns=order)), "",
             "## 5. 조건 하나씩 (True = 위)", ""]
    for col, name in (('k60', '코스피 > 60일선'), ('k20', '코스피 > 20일선'), ('breadth', '20일선 위 종목 ≥ 50%')):
        parts += [f"**{name}**", "", md(summarize(t, col)), "",
                  md(t.pivot_table(index=col, columns='구간', values='pnl', aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})")), ""]
    bins, labels = [-100, 3, 7, 12, 1000], ['3% 미만', '3~7%', '7~12%', '12% 이상']
    t['당일 상승률'] = pd.cut(t['day_pct'], bins, labels=labels, right=False)
    t['기준가까지 여유'] = pd.cut(t['cushion'], [-100, 5, 10, 15, 1000], labels=['5% 미만', '5~10%', '10~15%', '15% 이상'], right=False)
    parts += ["## 6. 신호 날 상승률별 (급등한 날 산 경우)", "", md(summarize(t, '당일 상승률')), "",
              md(t.pivot_table(index='당일 상승률', columns='verdict', values='pnl', observed=False,
                               aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})" if len(s) else "-").reindex(columns=order)), "",
              "구간별", "",
              md(t.pivot_table(index='당일 상승률', columns='구간', values='pnl', observed=False,
                               aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})" if len(s) else "-")), "",
              "## 7. 산 가격과 다음 날 기준가 사이 여유별 (여유가 클수록 매도 신호가 늦게 나온다)", "",
              md(summarize(t, '기준가까지 여유')), "", "구간별", "",
              md(t.pivot_table(index='기준가까지 여유', columns='구간', values='pnl', observed=False,
                               aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})" if len(s) else "-")), ""]
    def by_period(col):
        return md(t.pivot_table(index=col, columns='구간', values='pnl', observed=False,
                                aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})" if len(s) else "-"))

    t['시가 대비 종가'] = pd.cut(t['body'], [-100, -3, 0, 3, 1000], labels=['-3% 이하(크게 밀림)', '-3~0%(조금 밀림)', '0~3%', '3% 이상(종가까지 더 오름)'], right=False)
    t['돌파 폭'] = pd.cut(t['over'], [0, 1, 3, 7, 1000], labels=['1% 미만(겨우 넘음)', '1~3%', '3~7%', '7% 이상'], right=False)
    t['RSI(14)'] = pd.cut(t['rsi'], [0, 50, 70, 80, 101], labels=['50 미만', '50~70', '70~80', '80 이상'], right=False)
    t['MACD 오실레이터'] = t['osc'].map(lambda x: '확인불가' if pd.isna(x) else ('플러스' if x > 0 else '마이너스(0 포함)'))
    t['급등 7%+'] = t['day_pct'].map(lambda x: '신호 날 7% 이상' if x >= 7 else '신호 날 7% 미만')
    parts += ["## 8. 신호 날 장중 흐름: 시가 대비 종가 (장 초반 급등 후 버텼는지, 밀렸는지)", "",
              md(summarize(t, '시가 대비 종가')), "", "구간별 (거래당 평균%, 괄호는 거래 수)", "", by_period('시가 대비 종가'), "",
              "급등(신호 날 7% 이상)과 겹치는지", "",
              md(t.pivot_table(index='시가 대비 종가', columns='급등 7%+', values='pnl', observed=False,
                               aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})" if len(s) else "-")), "",
              "## 9. 종가가 20일 최고가를 넘은 폭", "",
              md(summarize(t, '돌파 폭')), "", "구간별", "", by_period('돌파 폭'), "",
              "## 10. 신호 날 RSI(14) (70 이상 = 흔히 '과열'로 보는 구간)", "",
              md(summarize(t, 'RSI(14)')), "", "구간별", "", by_period('RSI(14)'), "",
              "급등(신호 날 상승률)과 겹치는지 — 같은 상승률 칸 안에서 RSI별로 비교", "",
              md(t.pivot_table(index='당일 상승률', columns='RSI(14)', values='pnl', observed=False,
                               aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})" if len(s) else "-")), "",
              "장 판단별", "",
              md(t.pivot_table(index='RSI(14)', columns='verdict', values='pnl', observed=False,
                               aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})" if len(s) else "-").reindex(columns=order)), "",
              "## 11. 신호 날 MACD 오실레이터 (플러스 = 단기 상승 힘이 커지는 중)", "",
              md(summarize(t, 'MACD 오실레이터')), "", "구간별", "", by_period('MACD 오실레이터'), "",
              "RSI와 함께", "",
              md(t.pivot_table(index='RSI(14)', columns='MACD 오실레이터', values='pnl', observed=False,
                               aggfunc=lambda s: f"{s.mean():+.2f} ({len(s)})" if len(s) else "-")), ""]
    period = lambda d: '2024-09 이전' if d < pd.Timestamp('2024-10-01') else ('2024-10~' if d < pd.Timestamp('2025-10-01') else ('2025-10~' if d < pd.Timestamp('2026-10-01') else '2026-10~'))
    rows_all, rows_big, rows_period, rows_big_period = [], [], [], []
    for name, cmin, fast in EXIT_RULES:
        e = exit_trades(fr, sig, cmin, fast)
        e = e[e['date'] >= kospi.index.min() + pd.Timedelta(days=90)]  # 장 판단을 매길 수 있는 기간과 맞춤
        rows_all.append({'매도 규칙': name, **exit_stats(e)})
        rows_big.append({'매도 규칙': name, **exit_stats(e[e['cushion'] >= 15])})
        g = e.groupby(e['date'].map(period))['pnl']
        rows_period.append({'매도 규칙': name, **{k: f"{v.mean():+.2f} ({len(v)})" for k, v in g}})
        b = e[e['cushion'] >= 15]
        rows_big_period.append({'매도 규칙': name, **{f"{k} 평균 / 질 때": f"{v.mean():+.2f} / {v[v <= 0].mean():+.2f} ({len(v)})"
                                                    for k, v in b.groupby(b['date'].map(period))['pnl']}})
    parts += ["## 12. 급등 종목 매도 규칙 비교 (신호 날 산 가격과 다음 날 기준가 사이 여유가 클 때만 5일선으로 빨리 판다)", "",
              "- 여유 = 신호 날 종가가 직전 9거래일 종가 평균보다 몇 % 높은지. 클수록 10일선 매도 신호가 늦게 나온다.",
              "- 매도 규칙이 달라지면 같은 종목을 다시 사는 시점도 달라져 거래 수가 조금씩 다르다.", "",
              "전체 거래", "", md(pd.DataFrame(rows_all).set_index('매도 규칙')), "",
              "여유 15% 이상인 거래만", "", md(pd.DataFrame(rows_big).set_index('매도 규칙')), "",
              "구간별 거래당 평균% (괄호는 거래 수)", "", md(pd.DataFrame(rows_period).set_index('매도 규칙')), "",
              "여유 15% 이상인 거래만, 구간별 거래당 평균% / 질 때 평균% (괄호는 거래 수)", "",
              md(pd.DataFrame(rows_big_period).set_index('매도 규칙')), ""]
    skip_bad = t[t['verdict'] != '나쁨']
    rows_tier = []
    e = exit_trades(fr, sig, bs.FAST_EXIT_CUSHION, bs.FAST_EXIT_MA)
    e = e[e['date'] >= kospi.index.min() + pd.Timedelta(days=90)]
    for lag, name in ((1, '전날 수급까지 (15:10 예비 등급)'), (0, '당일 수급까지 (16:00 등급)')):
        tr = tiers(fr, lag)
        e['tier'] = [tr.at[d, c] for d, c in zip(e['date'], e['code'])]
        for tier in (1, 2, 3):
            b = e[e['tier'] == tier]
            g = b.groupby(b['date'].map(period))['pnl']
            rows_tier.append({'등급 기준': name, '등급': f"{tier}등급", **exit_stats(b),
                              **{k: f"{v.mean():+.2f} ({len(v)})" for k, v in g}})
    parts += ["## 13. 등급을 전날 수급으로 매길 때 vs 당일 수급으로 매길 때 (매도는 지금 규칙: 10일선, 급등 종목은 5일선)", "",
              "- 사는 가격은 둘 다 신호 날 15:30 종가. 16:00 등급을 보고 그 뒤 시간대에 사면 가격이 종가와 달라 이 수치와 달라진다.", "",
              md(pd.DataFrame(rows_tier).set_index(['등급 기준', '등급'])), ""]
    parts += ["## 14. '나쁨'일 때 안 샀다면", "",
              f"- 전부 샀을 때: {len(t)}건, 거래당 {t['pnl'].mean():+.2f}%, 합계 {t['pnl'].sum():+.0f}%",
              f"- 나쁨 제외: {len(skip_bad)}건, 거래당 {skip_bad['pnl'].mean():+.2f}%, 합계 {skip_bad['pnl'].sum():+.0f}%",
              "- 합계는 거래마다 같은 금액을 넣었다고 본 단순 합이다(동시에 들고 있는 종목 수 제한 없음)."]
    text = "\n".join(parts)
    print(text)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\n저장: {OUT_PATH}")
    return 0


def _has_tabulate():
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    sys.exit(main())
