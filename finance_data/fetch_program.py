import requests
from bs4 import BeautifulSoup
import json
import datetime


def get_program_trading(date_str):
    """
    네이버 금융 프로그램매매 동향(코스피)에서 차익/비차익/전체 순매수를 가져온다.

    실제 데이터 테이블은 https://finance.naver.com/sise/sise_program.naver 화면에
    iframe으로 삽입된 programDealTrendDay.naver 페이지에 있음 (2026-08-07 확인).
    표 구조: 시간 | 차익거래(매수,매도,순매수) | 비차익거래(매수,매도,순매수) | 전체(매수,매도,순매수)
    금액 단위는 억원이며, 아래에서 원(KRW) 단위로 환산해 반환한다.
    """
    headers = {'User-Agent': 'Mozilla/5.0'}
    bizdate = date_str.replace('-', '')
    target_label = bizdate[2:4] + '.' + bizdate[4:6] + '.' + bizdate[6:8]

    url = f"https://finance.naver.com/sise/programDealTrendDay.naver?bizdate={bizdate}&sosok="

    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'euc-kr'
    except Exception as e:
        return {'코스피_프로그램매매': f'확인불가: 요청 실패 - {e}'}

    if res.status_code != 200:
        return {'코스피_프로그램매매': f'확인불가: HTTP {res.status_code}'}

    soup = BeautifulSoup(res.text, 'lxml')
    table = soup.find('table', {'class': 'type_1'})
    if table is None:
        return {'코스피_프로그램매매': '확인불가: 페이지 구조 변경 가능성 (표를 찾을 수 없음)'}

    header_text = table.get_text()
    if '차익거래' not in header_text or '비차익거래' not in header_text:
        return {'코스피_프로그램매매': '확인불가: 페이지 구조 변경 가능성 (예상된 헤더 없음)'}

    try:
        target_row = None
        for tr in table.find_all('tr'):
            date_td = tr.find('td', {'class': 'date'})
            if date_td and date_td.text.strip() == target_label:
                target_row = tr
                break

        if target_row is None:
            return {'코스피_프로그램매매': f'확인불가: {target_label} 날짜 행을 찾을 수 없음'}

        tds = target_row.find_all('td')
        # date td + 9개 숫자 td (차익 매수/매도/순매수, 비차익 매수/매도/순매수, 전체 매수/매도/순매수)
        if len(tds) != 10:
            return {'코스피_프로그램매매': f'확인불가: 페이지 구조 변경 가능성 (컬럼 수 {len(tds)}개, 예상 10개)'}

        def to_krw(td_text):
            return int(td_text.strip().replace(',', '')) * 100_000_000  # 억원 -> 원

        arbitrage_net = to_krw(tds[3].text)
        non_arbitrage_net = to_krw(tds[6].text)
        total_net = to_krw(tds[9].text)

        return {
            '코스피_프로그램매매': {
                '날짜': date_str,
                '차익_순매수': arbitrage_net,
                '비차익_순매수': non_arbitrage_net,
                '프로그램_순매수': total_net
            }
        }
    except Exception as e:
        return {'코스피_프로그램매매': f'확인불가: 값 파싱 실패 - {e}'}


def _last_trading_date():
    """
    장 마감(15:30 KST) 이후면 오늘, 그 전이면 직전 거래일을 반환.
    주말뿐 아니라 한국 공휴일(대체공휴일 포함)도 pykrx의 실제 거래일 캘린더로 판단한다.
    (주말만 계산하던 예전 로직이 대체공휴일을 거래일로 착각해 조회 실패한 적이 있음)
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
    date = _last_trading_date()
    result = get_program_trading(date)
    with open("program_data.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
