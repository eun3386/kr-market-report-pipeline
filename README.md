# KR Market Report Pipeline

한국 주식 시장 데이터를 매일 모아서 정리하는 **개인용 데이터 파이프라인**이에요.
글로벌 지표·KRX 수급 데이터를 수집하고, 규칙 기반 추세 신호를 계산한 뒤, LLM이 정해진 템플릿대로 리포트를 쓰도록 자동화했어요.

> ⚠️ **면책 고지** — 개인 학습·연구용 코드예요. 어떤 종목의 매수·매도를 권유하지 않으며, 투자 자문이 아니에요.
> 백테스트 결과는 과거 데이터 기준이고 생존 편향 등 한계가 있어서 미래 수익을 보장하지 않아요.
> 실제 리포트·보유 정보는 이 저장소에 포함하지 않았어요.

## 하는 일

| 시각 (평일 KST) | 스크립트 | 내용 |
| --- | --- | --- |
| 07:20 | `run_premarket.sh` | 미국 지수·선물·환율·원자재 등 글로벌 지표 수집 → 장 시작 전 브리핑 |
| 15:10 | `run_closing_check.sh` | LLM 없이 Python만으로 장중 가격 기준 추세 돌파 후보·청산 대상 체크 |
| 16:00 | `run_all.sh` | KRX 시세·투자자별 수급·프로그램 매매·업종 데이터 수집 → 장 마감 리포트 |

각 작업은 macOS `launchd`로 예약 실행돼요.

## 구조

```
finance_data/
├── fetch_data.py            # yfinance·pykrx로 글로벌/국내 시세·수급 수집, 캐시 검증
├── fetch_kr_stocks.py       # 주요 종목 시세
├── fetch_program.py         # 프로그램 매매 동향
├── fetch_sectors.py         # 업종별 등락
├── fetch_scraped.py         # 네이버 금융 공개 시세 페이지 보조 데이터 (requests + BeautifulSoup)
├── breakout_signal.py       # 일목균형표 구름대 + 20일 고가 돌파 신호, 외국인 수급으로 등급화
├── closing_check.py         # 15:10 장중 체크 리포트
├── backtest_regime.py       # 신호·청산 규칙·장세 판단 백테스트 (결과: backtest_regime_result.md)
├── snapshot_validation.py   # 수집 데이터 날짜·형식 검증 (KST 기준)
├── publish_public_snapshot.py  # 검증된 공개 지표만 Supabase에 upsert (기본 dry-run)
├── pipeline_lock.py         # 여러 wrapper가 동시에 돌지 않게 하는 파일 잠금
├── run_*.sh                 # launchd가 부르는 wrapper
└── test_split_publication.py   # 단위 테스트 26개
```

## 설계에서 신경 쓴 점

- **실패하면 멈추기** — 데이터 수집이 실패하면 어제 데이터를 오늘 것처럼 재사용하지 않고 "확인불가"로 표시하거나 중단해요. 날짜·시간대(KST)까지 검증해요.
- **개인 정보와 공개 데이터 분리** — 공개 발행 스크립트는 개인 설정 파일을 아예 읽지 않아요. 실제 발행은 `--publish` 플래그와 환경변수 승인 게이트를 둘 다 통과해야 해요.
- **외부 API 부하 줄이기** — 종목별로 대량 조회하다 KRX 접속이 하루 차단된 일이 있어서, 시장 전체 하루치만 받아 로컬에 누적하는 방식으로 바꿨어요.
- **동시 실행 방지** — 공통 파일 잠금으로 아침·오후 작업이 겹쳐도 데이터가 꼬이지 않게 했어요.
- **운영 중 사고를 테스트로 고정** — 실행 순서 때문에 알림이 안 가던 문제, 엉뚱한 브랜치에 커밋되던 문제 등을 겪은 뒤 회귀 테스트로 막아 뒀어요.
- **LLM은 글쓰기만** — 숫자 계산·신호 판정은 Python이 하고, LLM(Claude CLI)은 검증된 JSON을 템플릿에 맞춰 문장으로 옮기는 역할만 해요.

## 실행

```bash
cd finance_data
python3 -m venv venv && venv/bin/pip install -r ../requirements.txt
cp ../portfolio_config.example.md ../portfolio_config.md   # 개인 설정 (git 제외)
venv/bin/python fetch_data.py
venv/bin/python -m unittest test_split_publication
```

- KRX 데이터 계정 정보는 `~/.krx_env`에서 읽어요.
- 리포트 작성 단계는 [Claude Code](https://claude.com/claude-code) CLI가 설치돼 있어야 해요.
- 선택: `DISCORD_WEBHOOK_URL`(완료 알림), `OBSIDIAN_VAULT_DIR`(리포트 사본 저장 위치).

## 기술

Python · pandas · pykrx · yfinance · BeautifulSoup · Supabase(PostgreSQL) · launchd · Bash · Claude Code CLI
