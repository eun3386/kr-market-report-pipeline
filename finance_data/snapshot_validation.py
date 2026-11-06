"""네트워크/자격증명 없이 공유하는 공개 발행 검증."""
import datetime
import math
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


def kst_now():
    return datetime.datetime.now(KST)


def valid_indicator(value):
    return isinstance(value, dict) and all(
        isinstance(value.get(key), (int, float))
        and not isinstance(value[key], bool) and math.isfinite(value[key])
        for key in ('close', 'pct_change')
    )


def has_global_values(data):
    return isinstance(data, dict) and any(valid_indicator(v) for v in data.values())


def valid_cache(cache, date_str):
    if not isinstance(cache, dict) or cache.get('date') != date_str:
        return False
    try:
        fetched = datetime.datetime.fromisoformat(cache['fetched_at'])
        if fetched.tzinfo is None or fetched.astimezone(KST).date().isoformat() != date_str:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    data = cache.get('global')
    # 일부 지표 실패도 오후에 다시 조회한다.
    return bool(data) and isinstance(data, dict) and all(valid_indicator(v) for v in data.values())


def validate_kr(market_data):
    today = kst_now().date().isoformat()
    flow = market_data.get('flow')
    if not isinstance(flow, dict):
        raise ValueError('KR 수급 데이터 없음')
    program = flow.get('코스피_프로그램매매')
    if not isinstance(program, dict) or program.get('날짜') != today:
        raise ValueError('오늘 KST 거래일 수급이 아님 — 이전 JSON 발행 거부')
    for market in ('코스피', '코스닥'):
        values = flow.get(market)
        if not isinstance(values, dict) or not all(
            isinstance(values.get(k), (int, float)) and not isinstance(values[k], bool)
            and math.isfinite(values[k]) for k in ('외국인', '기관합계', '개인')
        ):
            raise ValueError(f'{market} 수급 조회 미완료')
    for key in ('watchlist_candidates', 'technical_rebound_candidates'):
        if not isinstance(market_data.get(key), list):
            raise ValueError(f'{key} 조회 미완료')
    return today
