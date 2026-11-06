import yfinance as yf
import pandas as pd
import json
import datetime

def calculate_ichimoku(df):
    high_9 = df['High'].rolling(window=9).max()
    low_9 = df['Low'].rolling(window=9).min()
    tenkan_sen = (high_9 + low_9) / 2

    high_26 = df['High'].rolling(window=26).max()
    low_26 = df['Low'].rolling(window=26).min()
    kijun_sen = (high_26 + low_26) / 2

    senkou_span_a = ((tenkan_sen + kijun_sen) / 2).shift(25)
    
    high_52 = df['High'].rolling(window=52).max()
    low_52 = df['Low'].rolling(window=52).min()
    senkou_span_b = ((high_52 + low_52) / 2).shift(25)
    
    df['tenkan'] = tenkan_sen
    df['kijun'] = kijun_sen
    df['span_a'] = senkou_span_a
    df['span_b'] = senkou_span_b
    
    return df

def get_individual_stocks(date_str):
    targets = {
        '012450.KS': '한화에어로스페이스',
        '000660.KS': 'SK하이닉스',
        '267260.KS': 'HD현대일렉트릭',
        '111770.KS': '영원무역',
        '278470.KS': '에이피알'
    }
    
    results = {}
    end_date = pd.to_datetime(date_str) + pd.Timedelta(days=1)
    
    for code, name in targets.items():
        try:
            start_date = (pd.to_datetime(date_str) - pd.Timedelta(days=120)).strftime('%Y-%m-%d')
            target_date = end_date.strftime('%Y-%m-%d')
            
            df = yf.download(code, start=start_date, end=target_date, progress=False)
            
            if not df.empty:
                df = calculate_ichimoku(df)
                
                today_data = df.iloc[-1]
                prev_data = df.iloc[-2] if len(df) > 1 else today_data
                
                def get_val(series, col):
                    val = series[col]
                    return float(val.iloc[0]) if isinstance(val, pd.Series) else float(val)

                close = get_val(today_data, 'Close')
                prev_close = get_val(prev_data, 'Close')
                pct_change = (close - prev_close) / prev_close * 100
                
                span_a = get_val(today_data, 'span_a')
                span_b = get_val(today_data, 'span_b')
                upper_cloud = max(span_a, span_b)
                lower_cloud = min(span_a, span_b)
                
                if close > upper_cloud:
                    position = '구름대 위'
                elif close < lower_cloud:
                    position = '구름대 아래'
                else:
                    position = '구름대 안'
                
                results[name] = {
                    'close': int(close),
                    'pct_change': round(pct_change, 2),
                    'open': int(get_val(today_data, 'Open')),
                    'high': int(get_val(today_data, 'High')),
                    'low': int(get_val(today_data, 'Low')),
                    'volume': int(get_val(today_data, 'Volume')),
                    'tenkan': round(get_val(today_data, 'tenkan'), 2),
                    'kijun': round(get_val(today_data, 'kijun'), 2),
                    'cloud_upper': round(upper_cloud, 2),
                    'cloud_lower': round(lower_cloud, 2),
                    'cloud_pos': position
                }
            else:
                results[name] = '확인불가'
        except Exception as e:
            results[name] = f'확인불가: {str(e)}'
            
    return results

def _last_trading_date():
    """
    장 마감(15:30 KST) 이후면 오늘, 그 전이면 직전 거래일을 반환.
    주말뿐 아니라 한국 공휴일(대체공휴일 포함)도 pykrx의 실제 거래일 캘린더로 판단한다.
    """
    from pykrx import stock

    now = datetime.datetime.now()
    is_weekday = now.weekday() < 5
    after_close = now.time() >= datetime.time(15, 30)
    if is_weekday and after_close:
        candidate = now.date()
    else:
        candidate = now.date() - datetime.timedelta(days=1)

    nearest = stock.get_nearest_business_day_in_a_week(date=candidate.strftime('%Y%m%d'), prev=True)
    return f"{nearest[:4]}-{nearest[4:6]}-{nearest[6:]}"


if __name__ == "__main__":
    import json
    data = get_individual_stocks(_last_trading_date())
    with open("kr_stocks.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Done")
