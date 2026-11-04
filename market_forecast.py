#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
국장·미장 섹터 전망 대시보드 생성기

매일 실행하면:
  1) 지수·섹터 ETF·대표 종목 가격 수집 (yfinance)
  2) 뉴스 헤드라인 (Google News RSS), 국장 종목 외국인·기관 수급 (네이버 금융)
  3) 실적 발표일 + events.csv 경제 일정 정리
  4) 1일/1주/1개월/1년 상승 확률 계산 (적중률 기록으로 가중치 자동 조정)
  5) 급변 알림 (NTFY_TOPIC 설정 시 아이폰 푸시)
  6) docs/index.html 대시보드 생성

사용법
  python market_forecast.py           # 실데이터
  python market_forecast.py --demo    # 가짜 데이터로 화면만 확인
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import random
import re
import statistics as st
import subprocess
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("MF_OUT_DIR", BASE / "docs"))
HISTORY = BASE / "history.json"
WEIGHTS_FILE = BASE / "weights.json"
STATE_FILE = BASE / "state.json"
TEMPLATE = BASE / "template.html"
CLAUDE_MODEL = os.environ.get("MF_MODEL", "claude-haiku-5-5")
KST = ZoneInfo("Asia/Seoul")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def read_csv(name, cols):
    """assets.csv / stocks.csv / events.csv 읽기 (# 으로 시작하는 줄은 무시)"""
    p = BASE / name
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if r and not r[0].lstrip().startswith("#")]
    return [tuple(x.strip() for x in r[:cols]) for r in rows[1:] if len(r) >= cols]


MACRO = [("VIX 공포지수", "^VIX"), ("원/달러", "KRW=X"), ("미 10년물 금리", "^TNX")]
BENCH = {"KR": "^KS11", "US": "^GSPC"}  # 섹터 흐름 지도의 기준 지수

HORIZONS = {"day": 1, "week": 5, "month": 21, "year": 252}
SIGNALS = ("news", "rsi", "mom", "trend", "flow")
DEFAULT_WEIGHTS = {
    1:   {"news": .35, "rsi": .25, "mom": .20, "trend": .10, "flow": .10},
    5:   {"news": .25, "rsi": .20, "mom": .25, "trend": .20, "flow": .10},
    21:  {"news": .15, "rsi": .10, "mom": .30, "trend": .40, "flow": .05},
    252: {"news": .05, "rsi": .00, "mom": .35, "trend": .60, "flow": .00},
}
TILT = 0.15        # 신호가 확률을 얼마나 기울일지. 크게 잡을수록 과신함
LEARN_MIN = 200    # 채점된 기록이 이만큼 쌓이면 가중치 자동 조정 시작
ALERT_MOVE = {"asset": 0.03, "stock": 0.05}  # 하루 등락 알림 기준
VIX_ALERT = 25

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
    if hasattr(close, "columns"):
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


FLOW_DIAG = []


def _num(v):
    return int(str(v).replace(",", "").replace("+", "").strip() or 0)


def fetch_flows(ticker):
    """네이버 증권: 최근 5거래일 외국인·기관 순매매량 (국장 종목만)"""
    if not ticker.endswith((".KS", ".KQ")):
        return None
    import requests
    code = ticker.split(".")[0]
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/trend",
                         headers=UA, timeout=15)
        FLOW_DIAG.append(f"api {code} {r.status_code} {len(r.content)}")
        rows = r.json() if r.ok else []
        if isinstance(rows, dict):
            rows = next((v for v in rows.values() if isinstance(v, list)), [])
        out = []
        for row in rows:
            fk = next((k for k in row if "foreign" in k.lower() and "pure" in k.lower()), None)
            ok = next((k for k in row if "organ" in k.lower() and "pure" in k.lower()), None)
            dk = next((k for k in row if "date" in k.lower()), None)
            if fk and ok and dk:
                d = str(row[dk]).replace("-", "").replace(".", "")
                out.append((f"{d[:4]}.{d[4:6]}.{d[6:8]}", _num(row[ok]), _num(row[fk])))
        if len(out) >= 5:
            out = sorted(out, reverse=True)[:5]
            return {"dates": [o[0] for o in out], "inst": [o[1] for o in out],
                    "frgn": [o[2] for o in out]}
        if rows:
            FLOW_DIAG.append(f"api keys {list(rows[0])[:12]}")
    except Exception as e:
        FLOW_DIAG.append(f"api {code} err {e}")
    r = requests.get(f"https://finance.naver.com/item/frgn.naver?code={code}",
                     headers=UA, timeout=15)
    html = r.content.decode("euc-kr", errors="ignore")
    f = parse_flows(html)
    if not f:
        FLOW_DIAG.append(f"html {code} {r.status_code} {len(html)} tr={html.count('<tr')} {html[:120]!r}")
    return f


def parse_flows(html):
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>|&nbsp;|\s+", "", td)
                 for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) >= 7 and re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", cells[0]):
            try:
                rows.append((cells[0], int(cells[5].replace(",", "")),
                             int(cells[6].replace(",", ""))))
            except ValueError:
                pass
    if len(rows) < 5:
        return None
    rows = rows[:5]
    return {"dates": [r[0] for r in rows], "inst": [r[1] for r in rows],
            "frgn": [r[2] for r in rows]}


def flow_score(f):
    """5일 중 순매수일-순매도일 (외국인+기관) → -1~1"""
    if not f:
        return 0.0
    sgn = lambda v: (v > 0) - (v < 0)
    return round((sum(map(sgn, f["frgn"])) + sum(map(sgn, f["inst"]))) / 10, 3)


def fetch_earnings(ticker):
    import yfinance as yf
    cal = yf.Ticker(ticker).calendar
    dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not dates:
        return None
    d = dates[0]
    return d.isoformat()[:10] if hasattr(d, "isoformat") else str(d)[:10]


def fetch_info(ticker):
    """PER·PBR·배당 (yfinance)"""
    import yfinance as yf
    i = yf.Ticker(ticker).info or {}
    price = i.get("currentPrice") or i.get("regularMarketPrice") or i.get("previousClose")
    rate = i.get("dividendRate") or i.get("trailingAnnualDividendRate")
    ex = i.get("exDividendDate")
    if isinstance(ex, (int, float)):
        ex = dt.datetime.fromtimestamp(ex, dt.timezone.utc).date().isoformat()
    elif ex is not None:
        ex = str(ex)[:10]
    num = lambda v: float(v) if isinstance(v, (int, float)) and v == v else None
    return {"per": num(i.get("trailingPE")), "pbr": num(i.get("priceToBook")),
            "div": num(rate / price) if num(rate) and num(price) else None, "ex": ex}


def day_offsets(ds):
    d0 = dt.date.fromisoformat(ds[0])
    return [(dt.date.fromisoformat(d) - d0).days for d in ds]


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
    return (total / hits) * min(1.0, hits / 3)


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
        if items:
            prompt += f"[{tk}]\n" + "\n".join("- " + i["title"] for i in items) + "\n\n"
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 8000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=240)
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


def analyze(c, news_score, flow, weights):
    lr = [math.log(b / a) for a, b in zip(c[:-1], c[1:])]
    vol = st.stdev(lr[-60:])
    drift = max(min(st.mean(lr[-500:]), 0.0008), -0.0004)

    ret = lambda n: c[-1] / c[-1 - n] - 1
    ma20, ma60, ma120 = sma(c, 20), sma(c, 60), sma(c, 120)
    trend = sum(1 if x else -1 for x in (c[-1] > ma20, ma20 > ma60, ma60 > ma120)) / 3
    mom = math.tanh(ret(20) / (vol * math.sqrt(20)))
    r = rsi(c)
    sig = {"news": news_score, "rsi": -(r - 50) / 50, "mom": mom, "trend": trend,
           "flow": flow}

    forecast, scores = {}, {}
    for name, h in HORIZONS.items():
        s = sum(w * sig[k] for k, w in weights[h].items())
        scores[name] = round(s, 3)
        sd = vol * math.sqrt(h)
        m = drift * h + TILT * s * sd
        forecast[name] = {
            "p_up": round(ncdf(m / sd), 3),
            "exp": round(math.exp(m) - 1, 4),
            "lo": round(math.exp(m - sd) - 1, 4),
            "hi": round(math.exp(m + sd) - 1, 4),
        }
    return {
        "last": c[-1], "chg1d": ret(1),
        "ret": {"week": ret(5), "month": ret(21), "quarter": ret(60),
                "year": ret(min(252, len(c) - 1))},
        "rsi": round(r, 1), "vol_year": round(vol * math.sqrt(252), 4),
        "signals": {k: round(v, 3) for k, v in sig.items()},
        "forecast": forecast, "hist": [round(x, 2) for x in c[-504:]],
        "model": {"drift": round(drift, 6), "vol": round(vol, 5), "s": scores},
    }


# ─────────────────────── 적중률 기록 · 가중치 학습 ───────────────────────
H3 = (("day", 1, 0), ("week", 5, 1), ("month", 21, 2))


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def outcome(rec, h, prices):
    """기록의 기준일 이후 h거래일 뒤 올랐는지 (아직 모르면 None)"""
    dc = prices.get(rec["t"])
    if not dc or rec["d"] not in dc[0]:
        return None
    dates, c = dc
    i = dates.index(rec["d"])
    return None if i + h >= len(c) else c[i + h] > c[i]


def score_history(hist, prices, only):
    stats = {k: {"n": 0, "hit": 0, "sn": 0, "shit": 0} for k, _, _ in H3}
    for rec in hist:
        if rec["t"] not in only:
            continue
        for k, h, idx in H3:
            up = outcome(rec, h, prices)
            if up is None:
                continue
            p = rec["p"][idx]
            hit = int(up == (p > 0.5))
            s = stats[k]
            s["n"] += 1
            s["hit"] += hit
            if p >= 0.55 or p <= 0.45:
                s["sn"] += 1
                s["shit"] += hit
    return stats


def learn_weights(hist, prices):
    """신호별로 '방향을 맞힌 비율'을 보고 잘 맞는 신호의 비중을 키움"""
    weights = {h: dict(w) for h, w in DEFAULT_WEIGHTS.items()}
    report = {}
    for k, h, idx in H3:
        tally = {s: [0, 0] for s in SIGNALS}
        n = 0
        for rec in hist:
            up = outcome(rec, h, prices)
            if up is None:
                continue
            n += 1
            for s in SIGNALS:
                v = rec["s"].get(s, 0)
                if abs(v) >= 0.1:
                    tally[s][0] += 1
                    tally[s][1] += int((v > 0) == up)
        acc = {s: (round(t[1] / t[0], 3) if t[0] >= 50 else None) for s, t in tally.items()}
        active = n >= LEARN_MIN
        if active:
            base = DEFAULT_WEIGHTS[h]
            mult = {s: min(2.0, max(0.3, 1 + 10 * (acc[s] - 0.5))) if acc[s] is not None else 1.0
                    for s in base}
            raw = {s: base[s] * mult[s] for s in base}
            z = sum(raw.values()) or 1
            weights[h] = {s: round(v * sum(base.values()) / z, 3) for s, v in raw.items()}
        report[k] = {"n": n, "min": LEARN_MIN, "active": active, "acc": acc,
                     "weights": weights[h]}
    return weights, report


def append_history(hist, items):
    keys = {(a["ticker"], a["base_date"]) for a in items}
    hist = [r for r in hist if (r["t"], r["d"]) not in keys]
    for a in items:
        f = a["forecast"]
        hist.append({"t": a["ticker"], "d": a["base_date"],
                     "p": [f["day"]["p_up"], f["week"]["p_up"], f["month"]["p_up"]],
                     "s": a["signals"]})
    cutoff = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    return [r for r in hist if r["d"] >= cutoff]


# ─────────────────────────────── 일정 · 알림 ───────────────────────────────
def build_events(today, earnings):
    items = [{"date": d, "kind": k, "name": n, "desc": x}
             for d, k, n, x in read_csv("events.csv", 4)] + earnings
    start, end = today.isoformat(), (today + dt.timedelta(days=30)).isoformat()
    return sorted([e for e in items if start <= e["date"] <= end], key=lambda e: e["date"])


def mk_label(m):
    return "국장" if m == "KR" else "미장"


def build_alerts(assets, stocks, macro, events, state, today):
    alerts = []
    vix = next((m for m in macro if m["name"].startswith("VIX")), None)
    if vix and vix["value"] >= VIX_ALERT:
        alerts.append(f"VIX {vix['value']:.1f}: 시장이 공포 구간이에요")
    for kind, items in (("asset", assets), ("stock", stocks)):
        for a in items:
            if abs(a["chg1d"]) >= ALERT_MOVE[kind]:
                alerts.append(f"{a['name']}({mk_label(a['market'])}) 하루 {a['chg1d'] * 100:+.1f}%")
    prev, now = state.get("verdict", {}), {}
    for a in assets:
        p = a["forecast"]["week"]["p_up"]
        v = "up" if p >= .55 else "down" if p <= .45 else "flat"
        k = f"{a['market']}:{a['ticker']}"
        now[k] = v
        if prev.get(k) and v != "flat" and prev[k] != v:
            alerts.append(f"{a['name']}({mk_label(a['market'])}) 1주 전망이 "
                          f"{'상승' if v == 'up' else '하락'} 우위로 바뀌었어요")
    t, tm = today.isoformat(), (today + dt.timedelta(days=1)).isoformat()
    for e in events:
        if e["kind"] != "실적" and e["date"] in (t, tm):
            alerts.append(f"{'오늘' if e['date'] == t else '내일'} {e['name']}")
    return alerts, {"verdict": now}


def send_ntfy(alerts, url, title=None):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or not alerts:
        return
    import requests
    try:
        requests.post("https://ntfy.sh/", timeout=15, json={
            "topic": topic, "title": title or f"시장 전망 알림 {len(alerts)}건",
            "message": "\n".join("· " + a for a in alerts[:12]),
            "click": url, "tags": ["chart_with_upwards_trend"]})
    except Exception as e:
        print(f"[경고] 알림 전송 실패: {e}")


def quad_of(rs):
    if rs["x"] >= 0:
        return "선두" if rs["y"] >= 0 else "식는 중"
    return "살아나는 중" if rs["y"] >= 0 else "약세"


def build_weekly(results, stocks, hist, prices, snaps, today):
    pick = lambda a: {"key": f"{a['market']}:{a['ticker']}", "name": a["name"],
                      "ret": round(a["ret"]["week"], 4)}
    ranked = sorted(results + stocks, key=lambda a: a["ret"]["week"])
    start = (today - dt.timedelta(days=7)).isoformat()
    n = hit = 0
    for r in hist:
        if r["d"] >= start:
            up = outcome(r, 1, prices)
            if up is not None:
                n += 1
                hit += int(up == (r["p"][0] > 0.5))
    now_q = {f"{a['market']}:{a['ticker']}": quad_of(a["rs"]) for a in results if a.get("rs")}
    names = {f"{a['market']}:{a['ticker']}": f"{a['name']}({mk_label(a['market'])})" for a in results}
    old_days = sorted(d for d in snaps if d <= (today - dt.timedelta(days=6)).isoformat())
    moved = []
    if old_days:
        old = snaps[old_days[-1]]
        moved = [{"name": names[k], "from": old[k], "to": q}
                 for k, q in now_q.items() if old.get(k) and old[k] != q]
    snaps = dict(snaps)
    snaps[today.isoformat()] = now_q
    snaps = {d: snaps[d] for d in sorted(snaps)[-15:]}
    weekly = {"from": (today - dt.timedelta(days=7)).strftime("%m.%d"), "to": today.strftime("%m.%d"),
              "top": [pick(a) for a in ranked[::-1][:3]], "bottom": [pick(a) for a in ranked[:3]],
              "acc": {"n": n, "hit": hit}, "moved": moved}
    return weekly, snaps


def weekly_text(w):
    lines = ["오른 것: " + ", ".join(f"{x['name']} {x['ret'] * 100:+.1f}%" for x in w["top"]),
             "내린 것: " + ", ".join(f"{x['name']} {x['ret'] * 100:+.1f}%" for x in w["bottom"])]
    lines += [f"{m['name']}: {m['from']} → {m['to']}" for m in w["moved"][:4]]
    if w["acc"]["n"]:
        lines.append(f"1일 예측 적중률 {w['acc']['hit'] / w['acc']['n'] * 100:.0f}% ({w['acc']['n']}건)")
    return lines


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


def demo_news(name, kind):
    return [{"title": t.format(n=name), "link": ""}
            for t in (DEMO_NEWS if kind == "asset" else DEMO_NEWS[:2])]


def demo_flows(tk):
    if not tk.endswith((".KS", ".KQ")):
        return None
    rnd = random.Random(tk + "f")
    days = [(dt.date.today() - dt.timedelta(days=i)).strftime("%Y.%m.%d") for i in range(1, 6)]
    return {"dates": days, "inst": [rnd.randint(-90000, 90000) for _ in days],
            "frgn": [rnd.randint(-300000, 300000) for _ in days]}


def demo_earnings(tk, today):
    rnd = random.Random(tk + "e")
    return (today + dt.timedelta(days=rnd.randint(1, 40))).isoformat() if rnd.random() < .4 else None


def demo_info(tk):
    rnd = random.Random(tk + "i")
    return {"per": round(rnd.uniform(6, 40), 1), "pbr": round(rnd.uniform(.5, 5), 2),
            "div": round(rnd.uniform(0, .05), 4) if rnd.random() < .7 else None,
            "ex": (dt.date.today() + dt.timedelta(days=rnd.randint(3, 60))).isoformat() if rnd.random() < .3 else None}


# ──────────────────────────────── 실행 ────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 화면 확인")
    ap.add_argument("--push", action="store_true", help="생성 후 git push (PC 실행 시)")
    args = ap.parse_args()
    demo = args.demo
    today = dt.datetime.now(KST).date()
    repo = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("MF_REPO", "")

    assets_cfg = read_csv("assets.csv", 5)
    stocks_cfg = read_csv("stocks.csv", 5)
    universe = [("asset",) + r for r in assets_cfg] + [("stock",) + r for r in stocks_cfg]

    get_prices = demo_prices if demo else fetch_prices
    prices, news, flows, earnings, infos = {}, {}, {}, [], {}
    for kind, mk, grp, name, tk, q in universe:
        print(f"수집: {name} ({tk})")
        try:
            prices[tk] = get_prices(tk)
        except Exception as e:
            print(f"  [가격 실패] {e}")
            continue
        try:
            news[tk] = demo_news(name, kind) if demo else fetch_news(q, mk, 8 if kind == "asset" else 5)
        except Exception as e:
            news[tk] = []
            print(f"  [뉴스 실패] {e}")
        if kind != "stock":
            continue
        try:
            flows[tk] = demo_flows(tk) if demo else fetch_flows(tk)
        except Exception as e:
            print(f"  [수급 실패] {e}")
        try:
            d = demo_earnings(tk, today) if demo else fetch_earnings(tk)
            if d and d >= today.isoformat():
                earnings.append({"date": d, "kind": "실적", "name": f"{name} 실적 발표",
                                 "desc": mk_label(mk), "key": f"{mk}:{tk}"})
        except Exception as e:
            print(f"  [실적일 실패] {e}")
        try:
            infos[tk] = demo_info(tk) if demo else fetch_info(tk)
            ex = infos[tk].get("ex")
            if ex and ex >= today.isoformat():
                earnings.append({"date": ex, "kind": "배당락", "name": f"{name} 배당락일",
                                 "desc": mk_label(mk), "key": f"{mk}:{tk}"})
        except Exception as e:
            print(f"  [재무 실패] {e}")

    ai = None if demo else claude_sentiment(news)
    hist = [] if demo else [r for r in load_json(HISTORY, []) if "t" in r and "s" in r]
    weights, learn = learn_weights(hist, prices)

    def build(kind, mk, grp, name, tk):
        dates, c = prices.get(tk, ([], []))
        if len(c) < 130:
            print(f"  [건너뜀] {name}: 데이터 부족")
            return None
        if ai and tk in ai:
            score = max(-1.0, min(1.0, float(ai[tk].get("score", 0))))
            summary = ai[tk].get("summary", "")
        else:
            score, summary = lexicon_score([n["title"] for n in news.get(tk, [])]), ""
        f = flows.get(tk)
        a = analyze(c, score, flow_score(f), weights)
        key = f"{mk}:{tk}"
        a.update(market=mk, name=name, ticker=tk, base_date=dates[-1],
                 hist_start=dates[-len(a["hist"])], summary=summary, flows=f,
                 hd=day_offsets(dates[-len(a["hist"]):]),
                 news=news.get(tk, [])[:6 if kind == "asset" else 4],
                 earnings=next((e["date"] for e in earnings if e["key"] == key and e["kind"] == "실적"), None))
        a["group" if kind == "asset" else "sector"] = grp
        if tk in infos:
            ex = infos[tk].get("ex")
            a["div"] = {"yield": infos[tk].get("div"), "ex": ex if ex and ex >= today.isoformat() else None}
        return a

    results = [a for a in (build("asset", *r[:4]) for r in assets_cfg) if a]
    stocks = [a for a in (build("stock", *r[:4]) for r in stocks_cfg) if a]

    def sec_median(group, k):
        vals = [infos[s["ticker"]][k] for s in group
                if infos.get(s["ticker"], {}).get(k) and 0 < infos[s["ticker"]][k] < 300]
        return round(st.median(vals), 2) if vals else None

    for sec in {s["sector"] for s in stocks}:
        group = [s for s in stocks if s["sector"] == sec]
        per_m, pbr_m = sec_median(group, "per"), sec_median(group, "pbr")
        for s in group:
            i = infos.get(s["ticker"])
            if i and (i.get("per") or i.get("pbr")):
                s["val"] = {"per": round(i["per"], 2) if i.get("per") else None,
                            "pbr": round(i["pbr"], 2) if i.get("pbr") else None,
                            "per_sec": per_m, "pbr_sec": pbr_m}

    bench = {a["ticker"]: a for a in results}
    for a in results:
        b = bench.get(BENCH.get(a["market"]))
        if a["group"] == "섹터" and b:
            a["rs"] = {"x": round(a["ret"]["quarter"] - b["ret"]["quarter"], 4),
                       "y": round(a["ret"]["month"] - b["ret"]["month"], 4)}

    macro, fx = [], None
    for name, tk in MACRO:
        try:
            ds, c = get_prices(tk)
            if tk == "KRW=X" and len(c) > 10:
                fx = {"start": ds[-504:][0], "hd": day_offsets(ds[-504:]),
                      "c": [round(x, 2) for x in c[-504:]]}
            if len(c) >= 2:
                macro.append({"name": name, "value": round(c[-1], 2),
                              "chg": round(c[-1] / c[-2] - 1, 4)})
        except Exception as e:
            print(f"  [매크로 건너뜀] {name}: {e}")

    events = build_events(today, earnings)
    old_state = {} if demo else load_json(STATE_FILE, {})
    alerts, state = build_alerts(results, stocks, macro, events, old_state, today)
    weekly, state["quad_hist"] = build_weekly(results, stocks, hist, prices,
                                              old_state.get("quad_hist", {}), today)
    if demo:
        alerts = ["(데모) 반도체(국장) 하루 +5.2%", "(데모) 내일 미국 CPI 발표"]

    accuracy = score_history(hist, prices, {a["ticker"] for a in results})
    stock_accuracy = score_history(hist, prices, {s["ticker"] for s in stocks})
    if not demo:
        HISTORY.write_text(json.dumps(append_history(hist, results + stocks),
                                      ensure_ascii=False), encoding="utf-8")
        WEIGHTS_FILE.write_text(json.dumps(learn, ensure_ascii=False, indent=1), encoding="utf-8")
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        owner, _, name = repo.partition("/")
        url = f"https://{owner}.github.io/{name}/" if name else ""
        send_ntfy(alerts, url)
        if today.weekday() == 4 and dt.datetime.now(KST).hour >= 15:
            send_ntfy(weekly_text(weekly), url, "이번 주 시장 정리")

    now = dt.datetime.now(KST)
    data = {
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "generated_iso": now.isoformat(),
        "repo": repo,
        "demo": demo,
        "sentiment": "claude" if ai else "keyword",
        "macro": macro, "assets": results, "accuracy": accuracy,
        "stocks": stocks, "stock_accuracy": stock_accuracy,
        "events": events, "alerts": alerts, "learn": learn,
        "diag": FLOW_DIAG[:8],
        "weekly": weekly, "fx": fx,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"완료: {out} (알림 {len(alerts)}건)")

    if args.push:
        msg = f"update {data['generated']}"
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", msg], ["git", "push"]):
            subprocess.run(cmd, cwd=BASE, check=False)


if __name__ == "__main__":
    main()
