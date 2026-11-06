import requests
from bs4 import BeautifulSoup
import json
import datetime
import pandas as pd
from pykrx import stock


def _target_trading_date():
    """직전 완료된 거래일(YYYYMMDD). fetch_data.py의 _last_trading_date()와 동일 로직 —
    07:20 자동실행 시점(장 시작 전)엔 오늘이 아니라 어제(직전 거래일)를 가리켜야 함."""
    now = datetime.datetime.now()
    is_weekday = now.weekday() < 5
    after_close = now.time() >= datetime.time(15, 30)
    candidate = now.date() if (is_weekday and after_close) else now.date() - datetime.timedelta(days=1)
    return stock.get_nearest_business_day_in_a_week(date=candidate.strftime('%Y%m%d'), prev=True)


def _prev_trading_date(date_str):
    d = pd.to_datetime(date_str) - pd.Timedelta(days=1)
    return stock.get_nearest_business_day_in_a_week(date=d.strftime('%Y%m%d'), prev=True)


def fetch_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    results = {}
    
    # 1. 거래대금 상위 20위 (네이버 금융 - 거래대금 순위)
    try:
        url_vol = "https://finance.naver.com/sise/sise_quant_high.naver"
        res = requests.get(url_vol, headers=headers)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'lxml')
        
        table = soup.find('table', {'class': 'type_2'})
        top_vol = []
        if table:
            trs = table.find_all('tr')
            for tr in trs:
                if len(top_vol) >= 20:
                    break
                tds = tr.find_all('td')
                if len(tds) > 8:
                    name_tag = tds[1].find('a')
                    if name_tag:
                        name = name_tag.text.strip()
                        trade_val = tds[5].text.strip().replace(',', '') # 거래대금(백만) 컬럼인지 확인 (실제 네이버는 거래량 화면일수도 있으나 sise_quant_high는 거래대금임)
                        pct_change = tds[4].text.strip().replace('%', '').replace('+', '').replace('\n', '').strip()
                        top_vol.append({
                            'name': name,
                            'trade_val': trade_val,
                            'pct_change': pct_change
                        })
        results['top_vol'] = top_vol
    except Exception as e:
        results['top_vol'] = f"Error: {e}"
        
    # 2. 외국인 순매수 (네이버 금융 - 외국인 순매수)
    try:
        url_for = "https://finance.naver.com/sise/sise_deal_rank.naver"
        # 매매동향 페이지는 iframe 형태이거나 url 파라미터가 필요함.
        # https://finance.naver.com/sise/sise_deal_rank_iframe.naver?investor_etc_1=0&investor_etc_2=0&investor_etc_3=0&investor_etc_4=0&investor_etc_5=0&investor_etc_6=0&investor_etc_7=0&sosok=01
        url_for = "https://finance.naver.com/sise/sise_deal_rank_iframe.naver?sosok=01&investor_etc_1=0&investor_etc_2=0&investor_etc_3=0&investor_etc_4=0&investor_etc_5=0&investor_etc_6=0&investor_etc_7=0"
        res = requests.get(url_for, headers=headers)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'lxml')
        
        top_foreign = []
        # 네이버 금융 외국인 순매수는 테이블 구조 파악
        table = soup.find('table', {'class': 'type_2'})
        if table:
            trs = table.find_all('tr')
            for tr in trs:
                if len(top_foreign) >= 10:
                    break
                tds = tr.find_all('td')
                if len(tds) >= 4:
                    name_td = tds[1]
                    if name_td and name_td.text.strip():
                        name = name_td.text.strip()
                        # 보통 외국인 순매수 금액(백만)
                        val = tds[3].text.strip().replace(',', '')
                        top_foreign.append({
                            'name': name,
                            'net_buy': val
                        })
        results['top_foreign'] = top_foreign
    except Exception as e:
        results['top_foreign'] = f"Error: {e}"

    # 3. 업종 등락률 - 강세/약세 모두 수집 (pykrx 업종지수, 전일 종가 기준 공식 마감 데이터)
    #
    # 예전엔 네이버 테마 페이지를 크롤링했는데, 그 페이지는 "오늘 실시간가 vs 전일종가"를 보여줘서
    # 장 시작(09:00) 전인 07:20 자동실행 시점엔 오늘 가격이 아직 전일종가와 같아 전 테마가
    # 구조적으로 0.00%가 됨(크롤링 실패가 아니라 애초에 그 시간엔 이 페이지로 "어제 성과"를 알 수
    # 없는 것 — 매일 아침 오탐 발생 확인). pykrx 업종지수는 이미 마감된 공식 데이터라 조회 시간과
    # 무관하게 정확하다. 코스피/코스닥 지수 목록에는 "코스피"/"코스닥"이 이름에 들어간 규모별(대형주
    # 등)·스타일(코스피200 계열) 지수도 섞여 있어, 이름에 그 단어가 없는 순수 업종 지수만 남긴다.
    try:
        target_date = _target_trading_date()
        prev_date = _prev_trading_date(target_date)

        frames = []
        ticker_map = {}
        for market in ("KOSPI", "KOSDAQ"):
            df = stock.get_index_price_change_by_ticker(prev_date, target_date, market)
            df = df[~df.index.str.contains('코스피|코스닥')].copy()
            df['시장'] = market
            frames.append(df)
            for t in stock.get_index_ticker_list(target_date, market=market):
                ticker_map[(market, stock.get_index_ticker_name(t))] = t

        sectors_df = pd.concat(frames).sort_values('등락률', ascending=False)
        if sectors_df.empty:
            raise ValueError('업종지수 조회 결과가 비어있음')
        # 수십 종목을 가중평균한 실제 마감 지수가 전 업종 동시에 정확히 0.00%가 되는 건
        # 우연으로 설명 안 되는 이상 신호(개별 지수 하나가 정확히 0인 것조차 사실상 불가능) —
        # 데이터 조회 실패를 조용히 통과시키지 않도록 여기서도 방어한다.
        if (sectors_df['등락률'] == 0).all():
            raise ValueError('전 업종 등락률이 0 — 데이터 조회 실패로 추정')

        all_ohlcv = stock.get_market_ohlcv(target_date, market="ALL")

        def top_stocks(name, market):
            # 업종 구성종목 중 거래대금 상위 3종목을 대표종목으로 사용
            ticker = ticker_map.get((market, name))
            if not ticker:
                return []
            codes = stock.get_index_portfolio_deposit_file(ticker, target_date)
            rows = all_ohlcv.loc[all_ohlcv.index.intersection(codes)]
            rows = rows.sort_values('거래대금', ascending=False).head(3)
            return [{'name': stock.get_market_ticker_name(c), 'trade_val': int(r['거래대금'])} for c, r in rows.iterrows()]

        def build_entries(rows):
            return [
                {
                    'sector': name,
                    'updown': round(float(row['등락률']), 2),
                    'top_stocks': top_stocks(name, row['시장']),
                }
                for name, row in rows.iterrows()
            ]

        results['sectors'] = build_entries(sectors_df.head(5))
        results['weak_sectors'] = build_entries(sectors_df.tail(5).iloc[::-1])
    except Exception as e:
         results['sectors'] = f"Error: {e}"
         results['weak_sectors'] = f"Error: {e}"
         
    with open('scraped_data.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    fetch_data()
