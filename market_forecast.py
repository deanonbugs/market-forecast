#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
국장·미장 섹터 전망 대시보드 생성기

매일 1회 실행하면:
  1) 지수·섹터 ETF 가격 수집 (yfinance)
  2) 자산별 최근 뉴스 헤드라인 수집 (Google News RSS)
  3) 뉴스 심리 평가 (ANTHROPIC_API_KEY 있으면 Claude, 없으면 키워드 사전)
  4) 일/주/월/년 상승 확률과 예상 범위 계산
  5) 지난 예측의 적중률 채점 (history.json)
  6) docs/index.html 대시보드 생성 (폰에서 열람)

사용법
  python market_forecast.py           # 실데이터
  python market_forecast.py --demo    # 가짜 데이터로 화면만 확인
  python market_forecast.py --push    # 생성 후 GitHub Pages로 git push (PC 실행 시)
  ※ GitHub Actions(.github/workflows/update.yml)를 쓰면 PC 없이 매일 자동 실행
"""
import argparse
import datetime as dt
import json
import math
import os
import random
import re
import statistics as st
import subprocess
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote

BASE = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("MF_OUT_DIR", BASE / "docs"))
HISTORY = BASE / "history.json"
TEMPLATE = BASE / "template.html"
CLAUDE_MODEL = os.environ.get("MF_MODEL", "claude-haiku-5-5")

# 분석 대상은 assets.csv 에서 읽음 (아이폰 GitHub 앱/웹에서도 수정 가능)
def load_assets():
    import csv
    with open(BASE / "assets.csv", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if r and not r[0].lstrip().startswith("#")]
    return [tuple(x.strip() for x in r[:5]) for r in rows[1:] if len(r) >= 5]


MACRO = [("VIX 공포지수", "^VIX"), ("원/달러", "KRW=X"), ("미 10년물 금리", "^TNX")]

# 기간(거래일) 과 기간별 신호 가중치: 짧을수록 뉴스·과열, 길수록 추세 비중↑
HORIZONS = {"day": 1, "week": 5, "month": 21, "year": 252}
WEIGHTS = {
    1:   {"news": .40, "rsi": .30, "mom": .20, "trend": .10},
    5:   {"news": .30, "rsi": .20, "mom": .30, "trend": .20},
    21:  {"news": .15, "rsi": .10, "mom": .35, "trend": .40},
    252: {"news": .05, "rsi": .00, "mom": .35, "trend": .60},
}
TILT = 0.15  # 신호가 확률을 얼마나 기울일지. 크게 잡을수록 과신함

POS = ("상승 급등 강세 호조 반등 수혜 개선 흑자 돌파 신고가 최고 매수 회복 훈풍 "
       "surge surges rally rallies gain gains beat beats record upgrade rise rises "
       "jump jumps soar soars strong rebound").split()
NEG = ("하락 급락 약세 부진 우려 쇼크 적자 하향 매도 폭락 리스크 둔화 경고 위기 악재 "
       "fall falls drop drops slump miss misses downgrade fear fears plunge plunges "
       "weak selloff slide slides tumble tumbles").split()


# ───────────────────────────────── 수집 ─────────────────────────────────
def fetch_prices(ticker):
    import yfinance as yf
    df = yf.download(ticker, period="3y", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        return [], []
    close = df["Close"]
    if hasattr(close, "columns"):  # 신버전 yfinance 다중 컬럼 대응
        close = close.iloc[:, 0]
    close = close.dropna()
    return ([d.strftime("%Y-%m-%d") for d in close.index],
            [float(v) for v in close.values])


def fetch_news(query, market, limit=8):
    import feedparser
    loc = "hl=ko&gl=KR&ceid=KR:ko" if market == "KR" else "hl=en-US&gl=US&ceid=US:en"
    url = f"https://news.google.com/rss/search?q={quote(query + ' when:2d')}&{loc}"
    feed = feedparser.parse(url)
    return [{"title": e.get("title", ""), "link": e.get("link", "")}
            for e in feed.entries[:limit]]


# ─────────────────────────────── 뉴스 심리 ───────────────────────────────
def lexicon_score(titles):
    total, hits = 0.0, 0
    for t in titles:
        low = t.lower()
        words = set(re.findall(r"[a-z]+", low))
        has = lambda w: (w in words) if w.isascii() else (w in low)
        p, n = sum(map(has, POS)), sum(map(has, NEG))
        if p or n:
            total += (p - n) / (p + n)
            hits += 1
    if not hits:
        return 0.0
    return (total / hits) * min(1.0, hits / 3)  # 근거가 적으면 점수를 줄임


def claude_sentiment(news_by_ticker):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import requests
    prompt = ("아래는 자산별 최근 뉴스 헤드라인이다. 각 자산의 단기 주가에 대한 뉴스 심리를 "
              "-1(매우 부정)~1(매우 긍정) 사이 점수와 한국어 한 문장 요약으로 평가하라. "
              "헤드라인이 중립적이거나 엇갈리면 0에 가깝게 준다. "
              'JSON만 출력: {"티커": {"score": 숫자, "summary": "문장"}}\n\n')
    for tk, items in news_by_ticker.items():
        prompt += f"[{tk}]\n" + "\n".join("- " + i["title"] for i in items) + "\n\n"
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 4000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=180)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        return json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except Exception as e:
        print(f"[경고] Claude 심리 분석 실패 → 키워드 방식 사용: {e}")
        return None


# ──────────────────────────────── 분석 ────────────────────────────────
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def sma(c, n):
    return sum(c[-n:]) / n


def rsi(c, n=14):
    gain = loss = 0.0
    for a, b in zip(c[-n - 1:-1], c[-n:]):
        gain += max(b - a, 0)
        loss += max(a - b, 0)
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def analyze(c, news_score):
    lr = [math.log(b / a) for a, b in zip(c[:-1], c[1:])]
    vol = st.stdev(lr[-60:])
    drift = max(min(st.mean(lr[-500:]), 0.0008), -0.0004)  # 연 +22%/-10% 상한

    ret = lambda n: c[-1] / c[-1 - n] - 1
    ma20, ma60, ma120 = sma(c, 20), sma(c, 60), sma(c, 120)
    trend = sum(1 if x else -1 for x in (c[-1] > ma20, ma20 > ma60, ma60 > ma120)) / 3
    mom = math.tanh(ret(20) / (vol * math.sqrt(20)))
    r = rsi(c)
    sig = {"news": news_score, "rsi": -(r - 50) / 50, "mom": mom, "trend": trend}

    forecast = {}
    for name, h in HORIZONS.items():
        s = sum(w * sig[k] for k, w in WEIGHTS[h].items())
        sd = vol * math.sqrt(h)
        m = drift * h + TILT * s * sd
        forecast[name] = {
            "p_up": round(ncdf(m / sd), 3),
            "exp": round(math.exp(m) - 1, 4),
            "lo": round(math.exp(m - sd) - 1, 4),   # 약 68% 범위 하단
            "hi": round(math.exp(m + sd) - 1, 4),   # 약 68% 범위 상단
        }
    return {
        "last": c[-1], "chg1d": ret(1),
        "ret": {"week": ret(5), "month": ret(21), "year": ret(min(252, len(c) - 1))},
        "rsi": round(r, 1), "vol_year": round(vol * math.sqrt(252), 4),
        "signals": {k: round(v, 3) for k, v in sig.items()},
        "forecast": forecast, "spark": [round(x, 2) for x in c[-60:]],
    }


# ───────────────────────────── 적중률 기록 ─────────────────────────────
def load_history():
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return []


def score_history(hist, prices):
    stats = {k: {"n": 0, "hit": 0} for k in ("day", "week", "month")}
    for rec in hist:
        if rec["h"] not in stats or rec["ticker"] not in prices:
            continue
        dates, c = prices[rec["ticker"]]
        if rec["base_date"] not in dates:
            continue
        i = dates.index(rec["base_date"])
        j = i + HORIZONS[rec["h"]]
        if j >= len(c):
            continue  # 아직 결과가 나오지 않음
        stats[rec["h"]]["n"] += 1
        stats[rec["h"]]["hit"] += int((c[j] > c[i]) == (rec["p_up"] > 0.5))
    return stats


def append_history(hist, results):
    new_keys = {(r["ticker"], r["base_date"]) for r in results}
    hist = [h for h in hist if (h["ticker"], h["base_date"]) not in new_keys]
    for r in results:
        for h in ("day", "week", "month"):
            hist.append({"ticker": r["ticker"], "base_date": r["base_date"],
                         "h": h, "p_up": r["forecast"][h]["p_up"]})
    cutoff = (dt.date.today() - dt.timedelta(days=500)).isoformat()
    return [h for h in hist if h["base_date"] >= cutoff]


# ──────────────────────────────── 데모 ────────────────────────────────
def demo_prices(ticker, n=750):
    rnd = random.Random(ticker)
    start = {"^KS11": 2600, "^KQ11": 850, "^GSPC": 5800, "^IXIC": 18500,
             "^VIX": 16, "KRW=X": 1380, "^TNX": 4.2}.get(ticker, rnd.uniform(20, 300))
    mu, vol = rnd.uniform(-0.0002, 0.0007), rnd.uniform(0.008, 0.022)
    c = [start]
    for _ in range(n - 1):
        c.append(c[-1] * math.exp(rnd.gauss(mu, vol)))
    day, dates = dt.date.today(), []
    while len(dates) < n:
        day -= dt.timedelta(days=1)
        if day.weekday() < 5:
            dates.append(day.isoformat())
    return dates[::-1], c


DEMO_NEWS = ["{n} 업종, 외국인 순매수에 강세 마감", "{n} 관련주 실적 개선 기대감",
             "금리 인하 기대 속 {n} 반등", "{n} 업황 둔화 우려 여전"]


# ──────────────────────────────── 실행 ────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 화면 확인")
    ap.add_argument("--push", action="store_true", help="생성 후 git push")
    args = ap.parse_args()

    ASSETS = load_assets()
    get_prices = demo_prices if args.demo else fetch_prices
    prices, news = {}, {}
    for mk, grp, name, tk, q in ASSETS:
        print(f"수집: {name} ({tk})")
        try:
            prices[tk] = get_prices(tk)
            news[tk] = ([{"title": t.format(n=name), "link": ""} for t in DEMO_NEWS]
                        if args.demo else fetch_news(q, mk))
        except Exception as e:
            print(f"  [건너뜀] {e}")

    ai = None if args.demo else claude_sentiment(news)
    results = []
    for mk, grp, name, tk, q in ASSETS:
        dates, c = prices.get(tk, ([], []))
        if len(c) < 130:
            print(f"  [건너뜀] {name}: 데이터 부족")
            continue
        titles = [n["title"] for n in news.get(tk, [])]
        if ai and tk in ai:
            score = max(-1.0, min(1.0, float(ai[tk].get("score", 0))))
            summary = ai[tk].get("summary", "")
        else:
            score, summary = lexicon_score(titles), ""
        a = analyze(c, score)
        a.update(market=mk, group=grp, name=name, ticker=tk, base_date=dates[-1],
                 news=news.get(tk, [])[:6], summary=summary)
        results.append(a)

    macro = []
    for name, tk in MACRO:
        try:
            _, c = get_prices(tk)
            if len(c) >= 2:
                macro.append({"name": name, "value": round(c[-1], 2),
                              "chg": round(c[-1] / c[-2] - 1, 4)})
        except Exception as e:
            print(f"  [매크로 건너뜀] {name}: {e}")

    hist = [] if args.demo else load_history()
    accuracy = score_history(hist, prices)
    if not args.demo:
        HISTORY.write_text(json.dumps(append_history(hist, results), ensure_ascii=False),
                           encoding="utf-8")

    now = dt.datetime.now(ZoneInfo("Asia/Seoul"))
    data = {
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "generated_iso": now.isoformat(),
        "repo": os.environ.get("GITHUB_REPOSITORY") or os.environ.get("MF_REPO", ""),
        "demo": args.demo,
        "sentiment": "claude" if ai else "keyword",
        "macro": macro, "assets": results, "accuracy": accuracy,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"완료: {out}")

    if args.push:
        msg = f"update {data['generated']}"
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", msg], ["git", "push"]):
            subprocess.run(cmd, cwd=BASE, check=False)


if __name__ == "__main__":
    main()
