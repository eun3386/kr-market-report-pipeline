import requests
from bs4 import BeautifulSoup
import pandas as pd
import json

def get_strong_sectors():
    url = "https://finance.naver.com/sise/theme.naver"
    headers = {'User-Agent': 'Mozilla/5.0'}
    res = requests.get(url, headers=headers)
    soup = BeautifulSoup(res.content, 'lxml')
    
    themes = []
    table = soup.find('table', {'class': 'type_1 theme'})
    if table:
        trs = table.find_all('tr')
        for tr in trs:
            td_name = tr.find('td', {'class': 'col_type1'})
            if td_name:
                name = td_name.text.strip()
                link = "https://finance.naver.com" + td_name.find('a')['href']
                
                td_updown = tr.find('td', {'class': 'col_type2'})
                updown = td_updown.text.strip() if td_updown else "0.00%"
                
                themes.append({
                    'name': name,
                    'updown': float(updown.replace('%', '').replace('+', '')),
                    'link': link
                })
                
    # Sort by updown
    themes = sorted(themes, key=lambda x: x['updown'], reverse=True)[:5]
    
    # For each theme, get top 3 stocks by trading value (approximate by looking at the theme page)
    results = []
    for theme in themes:
        res_t = requests.get(theme['link'], headers=headers)
        soup_t = BeautifulSoup(res_t.content, 'lxml')
        
        stocks = []
        table_t = soup_t.find('table', {'class': 'type_5'})
        if table_t:
            trs_t = table_t.find_all('tr')
            for tr_t in trs_t:
                name_td = tr_t.find('td', {'class': 'name'})
                if name_td:
                    s_name = name_td.text.replace('*', '').strip()
                    # 거래대금은 현재 페이지 테이블에 없거나 위치가 다름.
                    # '거래대금' 열이 있는지 확인.
                    tds = tr_t.find_all('td')
                    # 네이버 금융 테마주 페이지 컬럼: 종목명, 현재가, 전일비, 등락률, 매수호가, 매도호가, 거래량, 거래대금, 전일거래량
                    if len(tds) >= 8:
                        trade_val_str = tds[7].text.strip().replace(',', '')
                        trade_val = int(trade_val_str) if trade_val_str.isdigit() else 0
                        stocks.append({
                            'name': s_name,
                            'trade_val': trade_val
                        })
                        
        stocks = sorted(stocks, key=lambda x: x['trade_val'], reverse=True)[:3]
        results.append({
            'sector': theme['name'],
            'updown': theme['updown'],
            'top_stocks': stocks
        })
        
    with open('sectors.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
        
if __name__ == "__main__":
    get_strong_sectors()
