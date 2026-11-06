from pykrx import stock
import pandas as pd

target_date = "20260806"
prev_date = "20260805"  # 확정된 이전 거래일도 같이 테스트

print("=== 1. dir(stock)에서 관련 함수 이름 검색 ===")
for name in dir(stock):
    if 'program' in name.lower() or 'investor' in name.lower():
        print(name)

print("\n=== 2. get_market_trading_value_by_investor 실제 구조 (8/5, 확정일) ===")
try:
    df = stock.get_market_trading_value_by_investor(prev_date, prev_date, "KOSPI")
    print("columns:", df.columns.tolist())
    print("index:", df.index.tolist())
    print(df)
except Exception as e:
    print("에러:", e)

print("\n=== 3. get_market_ohlcv(날짜, market=) 실제 구조 (8/6 vs 8/5) ===")
for d, label in [(target_date, "8/6"), (prev_date, "8/5(확정)")]:
    try:
        df = stock.get_market_ohlcv(d, market="KOSPI")
        print(f"[{label}] shape:", df.shape, "columns:", df.columns.tolist())
    except Exception as e:
        print(f"[{label}] 에러:", e)

print("\n=== 4. yfinance 실제로 어느 날짜 데이터를 주는지 확인 ===")
import yfinance as yf
df = yf.download("^SOX", start="2026-07-28", end="2026-08-07", progress=False)
print(df[['Close']].tail(6))

# 로그인이 이미 위쪽 코드에서 됐다는 가정하에, 전체 함수 목록에서 관련 키워드 검색
print("=== 프로그램매매 관련 함수 재검색 ===")
all_funcs = [n for n in dir(stock) if not n.startswith('_')]
for name in all_funcs:
    if any(kw in name.lower() for kw in ['program', 'trading', '프로그램']):
        print(name)

print("\n=== 전체 함수 목록 (참고용) ===")
for name in all_funcs:
    print(name)
