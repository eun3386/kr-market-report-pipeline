"""
market_data.json / scraped_data.json에서 개인정보 없는 공개용 필드만 추출해
글로벌 전용 테이블 또는 원자적 KR 발행 RPC로 전달한다.
기본 실행은 dry-run이다. 실제 저장은 --publish와
PUBLIC_SNAPSHOT_PUBLISH_ENABLED=1이 모두 있어야 한다.

주의: portfolio_config.md, reports/*.md는 이 스크립트에서 절대 읽지 않는다.
개인 포트폴리오 정보(평단가, 증권사명 등)가 이 코드 경로 자체에 존재하지 않게 하기 위함이다.

환경변수 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY는 ~/.premarket_supabase_publish_env에서
source해서 넘겨받는다 (git 추적 대상 아님).
"""
import json
import os
import argparse
import math
import datetime
import requests
from snapshot_validation import kst_now, valid_cache, validate_kr

DISCLAIMER_VERSION = "v1"


def _sanitize_nan(obj):
    """JSON 표준(NaN/Infinity 불허)에 맞게 재귀적으로 NaN/Infinity를 None으로 치환.
    fetch_data.py의 yfinance 조회가 가끔 NaN을 그대로 남길 때가 있어 방어적으로 처리."""
    if isinstance(obj, dict):
        return {k: _sanitize_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_nan(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _is_pullback_pattern(stock):
    cloud_pos = stock.get("cloud_pos")
    pct_change = stock.get("pct_change")
    close = stock.get("close")
    tenkan = stock.get("tenkan")
    if cloud_pos != "구름대 위" or pct_change is None or close is None or tenkan is None:
        return False
    return -3 <= pct_change <= -1 and close > tenkan


def _is_chase_warning(stock):
    cloud_pos = stock.get("cloud_pos")
    close = stock.get("close")
    high = stock.get("high")
    if cloud_pos != "구름대 위" or not close or not high:
        return False
    return (high - close) / high <= 0.01


def _direction_mismatch(stock):
    flag = stock.get("flag")
    return bool(flag and "방향 불일치" in flag)


def _watchlist_row(stock, snapshot_date):
    investor = stock.get("investor") if isinstance(stock.get("investor"), dict) else {}
    return {
        "snapshot_date": snapshot_date,
        "stock_name": stock.get("name"),
        "stock_code": stock.get("code"),
        "market": stock.get("market"),
        "overlap_reason": stock.get("overlap_reason"),
        "tracking_status": stock.get("tracking_status"),
        "close": stock.get("close"),
        "pct_change": stock.get("pct_change"),
        "cloud_position": stock.get("cloud_pos"),
        "is_pullback_pattern": _is_pullback_pattern(stock),
        "is_chase_warning": _is_chase_warning(stock),
        "direction_mismatch_flag": _direction_mismatch(stock),
        "foreign_net_buy": investor.get("외국인_순매수"),
        "inst_net_buy": investor.get("기관_순매수"),
        "individual_net_buy": investor.get("개인_순매수"),
    }


def _rebound_row(item, snapshot_date):
    investor = item.get("investor") if isinstance(item.get("investor"), dict) else {}
    return {
        "snapshot_date": snapshot_date,
        "stock_name": item.get("name"),
        "stock_code": item.get("code"),
        "high_120d": item.get("고점_120일"),
        "drawdown_pct": item.get("고점대비_하락률"),
        "close": item.get("close"),
        "pct_change": item.get("pct_change"),
        "cloud_position": item.get("cloud_pos"),
        "foreign_net_buy": investor.get("외국인_순매수"),
        "inst_net_buy": investor.get("기관_순매수"),
        "individual_net_buy": investor.get("개인_순매수"),
    }


def build_public_payload(market_data, scraped_data, snapshot_date):
    flow = market_data.get("flow", {})

    strong_sectors = None
    weak_sectors = None
    if isinstance(scraped_data, dict):
        sectors = scraped_data.get("sectors")
        weak = scraped_data.get("weak_sectors")
        if isinstance(sectors, list) and any(s.get("updown") for s in sectors):
            strong_sectors = sectors
        if isinstance(weak, list) and any(s.get("updown") for s in weak):
            weak_sectors = weak

    today = kst_now().date()
    snap_date = datetime.datetime.strptime(snapshot_date, "%Y-%m-%d").date()
    data_lag_days = (today - snap_date).days

    # 글로벌 값 보존 및 구버전용 복사는 RPC가 트랜잭션 안에서 담당한다.
    snapshot_row = {
        "snapshot_date": snapshot_date,
        # 같은 거래일을 다시 발행해도 앱의 "업데이트" 시각이 실제 발행 시각을
        # 보여주도록 upsert 때 published_at도 함께 갱신한다.
        "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "flow": flow,
        "strong_sectors": strong_sectors,
        "weak_sectors": weak_sectors,
        "data_lag_days": data_lag_days,
        "disclaimer_version": DISCLAIMER_VERSION,
    }

    watchlist_candidates = market_data.get("watchlist_candidates", [])
    watchlist_rows = [_watchlist_row(s, snapshot_date) for s in watchlist_candidates if isinstance(s, dict) and s.get("code")]

    rebound_candidates = market_data.get("technical_rebound_candidates", [])
    rebound_rows = [_rebound_row(item, snapshot_date) for item in rebound_candidates if isinstance(item, dict) and item.get("code")]

    return snapshot_row, watchlist_rows, rebound_rows


def _global_row(cache):
    if not valid_cache(cache, kst_now().date().isoformat()):
        return None
    return {
        "snapshot_date": cache["date"],
        "global_indicators": cache["global"],
        "global_indicators_published_at": cache["fetched_at"],
    }


def _send(path, payload, *, write=False):
    if not write:
        print(f"DRY_RUN: 로컬 입력 검증 완료 — {path} (네트워크 요청/DB 저장 없음)")
        return 0
    if os.environ.get("PUBLIC_SNAPSHOT_PUBLISH_ENABLED") != "1":
        print("발행 차단: PUBLIC_SNAPSHOT_PUBLISH_ENABLED=1 승인 게이트 필요")
        return 1
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url.startswith("https://") or not service_key:
        print("발행 차단: HTTPS SUPABASE_URL 또는 SUPABASE_SERVICE_ROLE_KEY 설정 필요")
        return 1
    try:
        res = requests.post(
            f"{supabase_url}/rest/v1/{path}",
            headers={
                "apikey": service_key, "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=payload, timeout=15, allow_redirects=False,
        )
        # 리다이렉트를 따라 자격증명을 전달하거나 3xx를 성공으로 취급하지 않는다.
        if not 200 <= res.status_code < 300:
            print(f"확인불가: Supabase 발행 실패 (HTTP {res.status_code})")
            return 1
    except requests.RequestException as error:
        # 요청 URL/헤더/응답 본문 등 비밀값이 섞일 수 있는 원문은 출력하지 않는다.
        print(f"확인불가: Supabase 발행 실패 ({type(error).__name__})")
        return 1
    return 0


def publish_global_only(*, write=False):
    """글로벌 전용 행은 기존 KR 테이블에 생성하지 않는다."""
    try:
        snapshot_row = _global_row(_load_json("global_data.json"))
    except (OSError, ValueError):
        snapshot_row = None
    if snapshot_row is None:
        print("확인불가: 오늘 KST 기준의 정상 글로벌 캐시 없음 — 발행 중단")
        return 1
    result = _send("public_global_snapshots?on_conflict=snapshot_date", snapshot_row, write=write)
    if result == 0:
        print(f"글로벌 {'발행' if write else '입력 검증'} 완료: {snapshot_row['snapshot_date']}")
    return result


def publish(*, write=False):
    try:
        market_data = _load_json("market_data.json")
        scraped_data = _load_json("scraped_data.json")
        if not isinstance(market_data, dict):
            raise ValueError("market_data.json 없음 또는 객체 형식 아님")
        snapshot_date = validate_kr(market_data)
        snapshot_row, watchlist_rows, rebound_rows = build_public_payload(market_data, scraped_data, snapshot_date)
    except (OSError, ValueError, TypeError, AttributeError):
        print("확인불가: KR 입력 파일/날짜/수급 형식 검증 실패 — 발행 중단")
        return 1

    fallback = _global_row({
        "date": market_data.get("global_snapshot_date"),
        "fetched_at": market_data.get("global_fetched_at"),
        "global": market_data.get("global"),
    })
    if fallback is None:
        print("주의: 정상 글로벌 fallback 없음 — KR만 검증/발행, DB의 기존 글로벌 값 유지")
    payload = _sanitize_nan({
        "p_snapshot": snapshot_row, "p_watchlist": watchlist_rows,
        "p_rebound": rebound_rows, "p_global_fallback": fallback,
    })
    result = _send("rpc/publish_kr_snapshot", payload, write=write)
    if result == 0:
        print(f"KR {'발행' if write else '입력 검증'} 완료: {snapshot_date} "
              f"(watchlist {len(watchlist_rows)}건, rebound {len(rebound_rows)}건)")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="공개 스냅샷: 기본 dry-run, 실제 저장은 별도 승인 필요")
    parser.add_argument("--global-only", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--publish", action="store_true", help="승인 게이트 확인 후 실제 저장")
    mode.add_argument("--dry-run", action="store_true", help="로컬 입력만 검증 (기본값)")
    args = parser.parse_args(argv)
    return (publish_global_only if args.global_only else publish)(write=args.publish)


if __name__ == "__main__":
    raise SystemExit(main())
