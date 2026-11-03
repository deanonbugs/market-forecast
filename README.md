# 국장·미장 섹터 전망 대시보드

평일 아침마다 코스피·코스닥·S&P500·나스닥과 섹터별(반도체, 2차전지 등) 1일/1주/1개월/1년 상승 확률을 계산해 대시보드로 만듭니다. GitHub에 올려 두면 **PC를 켜 두지 않아도** 클라우드에서 자동 실행되고, 아이폰에서 보고 조작할 수 있습니다.

## 폴더 구성

- `market_forecast.py` 수집·분석·화면 생성
- `template.html` 대시보드 디자인
- `assets.csv` 분석할 지수·섹터 목록 (폰에서도 수정 가능)
- `.github/workflows/update.yml` 매일 자동 실행 설정
- `docs/` 대시보드가 만들어지는 곳 (아이콘 포함)

## 1. GitHub에 올리기 (최초 1회, PC에서)

1. github.com에서 새 저장소 생성 (예: `market-forecast`, Public)
2. 이 폴더 전체를 업로드 (`.github` 폴더 포함 — 웹 업로드 시 숨김 폴더가 빠지지 않게 주의)
3. Settings → Pages → Source `Deploy from a branch`, Branch `main` / 폴더 `/docs` → Save
4. Settings → Actions → General → Workflow permissions → `Read and write permissions` → Save
5. (선택) Settings → Secrets and variables → Actions → `ANTHROPIC_API_KEY` 등록 → 뉴스를 Claude가 요약
6. Actions 탭 → `시장 전망 업데이트` → `Run workflow`로 첫 실행 (2~3분)

이후 한국시간 **월~금 오전 7시**에 자동 실행됩니다.

## 2. 아이폰 홈 화면에 앱처럼 추가

1. Safari에서 `https://아이디.github.io/market-forecast/` 열기
2. 공유 버튼 → `홈 화면에 추가`

주소창 없이 앱처럼 열립니다. 화면에서 할 수 있는 것:

- 국장/미장 버튼 또는 **좌우로 밀어서** 시장 전환
- 1일·1주·1개월·1년 기간 선택
- ★로 관심 섹터 등록 → 위로 고정, ★ 버튼으로 관심만 보기
- 마지막으로 본 시장·기간·관심 목록은 기억됨
- `새로고침` 최신 결과 다시 불러오기
- `지금 업데이트` GitHub 실행 화면으로 이동 → `Run workflow` 누르면 즉시 재계산
- `섹터 편집` assets.csv 편집 화면으로 이동 → 줄 추가/삭제 후 Commit

`지금 업데이트`와 `섹터 편집`은 GitHub 로그인이 필요합니다. GitHub 앱을 설치해 두면 편합니다.

## 3. 원탭 업데이트 단축어 (선택)

GitHub 화면을 거치지 않고 버튼 하나(또는 "시리야, 시장 업데이트")로 실행하려면:

1. GitHub → Settings → Developer settings → Fine-grained tokens → Generate
   - Repository access: 이 저장소만 / Permissions: **Actions = Read and write**
2. 아이폰 `단축어` 앱 → 새 단축어 → 이름 `시장 업데이트`
3. 동작 추가: `URL` → `https://api.github.com/repos/아이디/market-forecast/actions/workflows/update.yml/dispatches`
4. 동작 추가: `URL의 콘텐츠 가져오기`
   - 방법 `POST`
   - 헤더 `Authorization` = `Bearer 발급받은토큰`, `Accept` = `application/vnd.github+json`
   - 요청 본문 `JSON`, 키 `ref` = `main`
5. 동작 추가: `알림 표시` → "업데이트 시작, 3분 뒤 확인"

토큰은 단축어 안에만 두고 대시보드나 저장소에 넣지 마세요.

## 4. 섹터 바꾸기

`assets.csv`에 `시장,구분,이름,티커,뉴스검색어` 형식으로 한 줄 추가합니다. 한국 ETF·코스피 종목은 `005930.KS`, 코스닥 종목은 `247540.KQ`처럼 접미사를 붙입니다. 티커가 틀리면 그 줄만 건너뜁니다.

## 5. PC에서 직접 실행 (선택)

```
pip install -r requirements.txt
python market_forecast.py --demo   # 가짜 데이터로 화면 확인
python market_forecast.py --push   # 실데이터 생성 후 GitHub에 올림
```

## 6. 예측 방식

| 신호 | 의미 | 단기 비중 | 장기 비중 |
|---|---|---|---|
| 뉴스 심리 | 최근 2일 헤드라인 | 높음 | 낮음 |
| RSI | 과열이면 조정 가능성 | 높음 | 없음 |
| 모멘텀 | 최근 20일 수익률의 강도 | 중간 | 중간 |
| 추세 | 20·60·120일 이동평균 배열 | 낮음 | 높음 |

신호를 합친 점수로 과거 변동성 기반 분포를 살짝 기울여 상승 확률과 약 68% 범위를 냅니다. 일부러 보수적으로 설계해 확률이 40~60%를 잘 벗어나지 않습니다. 매일 예측을 `history.json`에 저장하고 실제 결과로 채점하니, 한두 달 쌓인 뒤 `WEIGHTS`와 `TILT`를 조정하세요.
