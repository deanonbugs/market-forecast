# -*- coding: utf-8 -*-
"""부동산: 국토부 아파트 매매 실거래가(상세) + 지역 흐름 + 뉴스 → 시세 추정과 전망"""
import csv
import datetime as dt
import json
import math
import random
import statistics as st
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = BASE / "realestate_cache.json"
API = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
MONTHS = 36    # 모아둘 기간 (개월)
REFRESH = 3    # 매번 다시 받는 최근 개월 수 (늦게 신고되는 거래 반영)
HZ = {"m3": 3, "m6": 6, "m12": 12}
W = {"news": .25, "trend": .30, "mom": .25, "volume": .20}
TILT = 0.2
F = {"apt": ("aptNm", "아파트"), "dong": ("umdNm", "법정동"), "area": ("excluUseAr", "전용면적"),
     "amt": ("dealAmount", "거래금액"), "y": ("dealYear", "년"), "m": ("dealMonth", "월"),
     "d": ("dealDay", "일"), "floor": ("floor", "층"), "built": ("buildYear", "건축년도"),
     "cancel": ("cdealType", "해제여부")}


def read_apts():
    p = BASE / "apartments.csv"
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if r and not r[0].lstrip().startswith("#")]
    out = []
    for r in rows[1:]:
        r = [x.strip() for x in r] + [""] * 6
        if r[0] and r[1]:
            out.append({"name": r[0], "code": r[1], "region": r[2] or r[1], "dong": r[3],
                        "kw": r[4] or r[0], "area": r[5]})
    return out


def months_back(n, today):
    y, m, out = today.year, today.month, []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def _get(it, key):
    for name in F[key]:
        el = it.find(name)
        if el is not None and el.text is not None:
            return el.text.strip()
    return ""


def fetch_month(key, code, ym):
    import requests
    items, page = [], 1
    while True:
        r = requests.get(API, timeout=30, params={"serviceKey": key, "LAWD_CD": code,
                                                  "DEAL_YMD": ym, "pageNo": page, "numOfRows": 1000})
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise RuntimeError(f"응답 해석 실패 ({r.status_code}): {r.text[:120]}")
        rc = (root.findtext(".//resultCode") or "").strip()
        if rc not in ("000", "00"):
            msg = (root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or r.text[:120]).strip()
            raise RuntimeError(f"API 오류 {rc}: {msg}")
        for it in root.iter("item"):
            if _get(it, "cancel").upper() == "O":
                continue
            try:
                amt = int(_get(it, "amt").replace(",", ""))
                area = float(_get(it, "area"))
                d = f"{int(_get(it, 'y')):04d}-{int(_get(it, 'm')):02d}-{int(_get(it, 'd')):02d}"
            except ValueError:
                continue
            items.append({"apt": _get(it, "apt"), "dong": _get(it, "dong"), "area": area, "amt": amt,
                          "date": d, "floor": _get(it, "floor"), "built": _get(it, "built")})
        total = int((root.findtext(".//totalCount") or "0").strip() or 0)
        if page * 1000 >= total:
            return items
        page += 1


def collect(key, codes, today, diag):
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    yms = months_back(MONTHS, today)
    for code in codes:
        c = cache.setdefault(code, {})
        got = 0
        for ym in yms:
            if ym in c and ym not in yms[-REFRESH:]:
                continue
            c[ym] = fetch_month(key, code, ym)
            got += 1
        for ym in list(c):
            if ym not in yms:
                del c[ym]
        diag.append(f"{code}: {got}개월 새로 받음, 총 {sum(len(v) for v in c.values())}건")
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return cache


def ym_of(date):
    return date[:4] + date[5:7]


def region_index(months):
    rows = []
    for ym in sorted(months):
        ppa = [t["amt"] / t["area"] for t in months[ym] if t["area"] > 0]
        rows.append({"ym": ym, "ppa": round(st.median(ppa), 1) if ppa else None, "n": len(ppa)})
    return rows


def smooth(rows):
    vals, out = [r["ppa"] for r in rows], {}
    for i, r in enumerate(rows):
        win = [v for v in vals[max(0, i - 2):i + 1] if v]
        out[r["ym"]] = sum(win) / len(win) if win else None
    return out


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def forecast(rows, news_score):
    base = rows[:-1]  # 이번 달은 신고가 덜 들어와서 제외
    sm = smooth(base)
    seq = [sm[r["ym"]] for r in base if sm[r["ym"]]]
    if len(seq) < 13:
        return None, {}
    lr = [math.log(b / a) for a, b in zip(seq[:-1], seq[1:])]
    raw = [r["ppa"] for r in base if r["ppa"]]
    lr_raw = [math.log(b / a) for a, b in zip(raw[:-1], raw[1:])]
    drift = 0.5 * max(min(st.mean(lr[-24:]), 0.01), -0.01)   # 과거 추세는 절반만 믿음
    vol = max(st.stdev(lr_raw[-24:]), 0.01)                    # 월 1% 이상으로 보수적으로
    trend = max(-1.0, min(1.0, (st.mean(seq[-3:]) / st.mean(seq[-12:]) - 1) / (vol * 3)))
    mom = math.tanh((seq[-1] / seq[-7] - 1) / (vol * math.sqrt(6)))
    ns = [r["n"] for r in base]
    volume = math.tanh(st.mean(ns[-3:]) / (st.mean(ns[-12:]) or 1) - 1)
    sig = {"news": news_score, "trend": trend, "mom": mom, "volume": volume}
    s = sum(W[k] * sig[k] for k in W)
    fc = {}
    for k, h in HZ.items():
        sd = vol * math.sqrt(h)
        m = drift * h + TILT * s * sd
        fc[k] = {"p_up": round(ncdf(m / sd), 3), "exp": round(math.exp(m) - 1, 4),
                 "lo": round(math.exp(m - sd) - 1, 4), "hi": round(math.exp(m + sd) - 1, 4)}
    sig = {k: round(v, 3) for k, v in sig.items()}
    sig.update(ret3=round(seq[-1] / seq[-4] - 1, 4), ret12=round(seq[-1] / seq[-13] - 1, 4))
    return fc, sig


def apt_view(a, months, sm, now_ix):
    allt = [t for ym in sorted(months) for t in months[ym]]
    in_dong = [t for t in allt if not a["dong"] or a["dong"] in t["dong"]]
    trades = [t for t in in_dong if a["kw"] in t["apt"]]
    if a["area"]:
        try:
            target = float(a["area"])
            trades = [t for t in trades if abs(t["area"] - target) < 3]
        except ValueError:
            pass
    trades.sort(key=lambda t: t["date"])
    groups = {}
    for t in trades:
        groups.setdefault(round(t["area"]), []).append(t)
    areas = []
    for ar, ts in sorted(groups.items()):
        adj = [t["amt"] * now_ix / sm[ym_of(t["date"])] for t in ts[-5:]
               if now_ix and sm.get(ym_of(t["date"]))]
        series = {}
        for t in ts:
            series.setdefault(ym_of(t["date"]), []).append(t["amt"])
        areas.append({"area": ar, "n": len(ts), "est": round(st.median(adj)) if adj else ts[-1]["amt"],
                      "last": ts[-1], "first": ts[0]["date"],
                      "series": [{"ym": ym, "p": round(st.median(v)), "n": len(v)} for ym, v in sorted(series.items())]})
    return {"name": a["name"], "code": a["code"], "region": a["region"], "dong": a["dong"], "kw": a["kw"],
            "built": next((t["built"] for t in trades if t["built"]), ""),
            "areas": areas, "trades": [dict(t, area=round(t["area"], 2)) for t in trades[-30:][::-1]],
            "dong_names": [] if trades else sorted({t["apt"] for t in in_dong})[:30]}


def demo_months(code, today):
    rnd, out, lvl = random.Random(code), {}, 7000.0
    names = ["대청", "개포자이", "래미안블레스티지", "디에이치아너힐즈"]
    for ym in months_back(MONTHS, today):
        lvl *= math.exp(rnd.gauss(0.004, 0.012))
        rows = []
        for _ in range(rnd.randint(150, 400)):
            area = rnd.choice([39.5, 49.9, 60.8, 84.9, 114.2])
            nm = rnd.choice(names)
            amt = int(lvl * area * rnd.uniform(.85, 1.15) * (0.8 if nm == "대청" else 1.1) / 100) * 100
            rows.append({"apt": nm, "dong": "개포동", "area": area, "amt": amt,
                         "date": f"{ym[:4]}-{ym[4:]}-{rnd.randint(1, 28):02d}",
                         "floor": str(rnd.randint(1, 15)), "built": "1992" if nm == "대청" else "2019"})
        out[ym] = rows
    return out


def build(key, fetch_news, lexicon_score, today, demo=False):
    apts = read_apts()
    if not apts:
        return None
    diag, res = [], {"updated": today.isoformat(), "regions": [], "apts": [], "error": ""}
    codes = sorted({a["code"] for a in apts})
    if demo:
        cache = {c: demo_months(c, today) for c in codes}
    elif not key:
        res["error"] = "GitHub 비밀값 DATA_GO_KR_KEY가 없어요."
        return res
    else:
        try:
            cache = collect(key, codes, today, diag)
        except Exception as e:
            res["error"], res["diag"] = str(e)[:200], diag
            return res
    for code in codes:
        months = cache.get(code, {})
        rows = region_index(months)
        name = next(a["region"] for a in apts if a["code"] == code)
        queries = [f"{name} 아파트값", f"{name} 부동산"] + [f"{a['dong']} {a['kw']}" for a in apts if a["code"] == code]
        news, seen = [], set()
        for q in queries:
            try:
                items = [] if demo else fetch_news(q, "KR", 5)
            except Exception:
                items = []
            for n in items:
                if n["title"] not in seen:
                    seen.add(n["title"])
                    news.append(n)
        if demo:
            news = [{"title": f"{name} 아파트값 상승폭 확대", "link": ""}, {"title": "대출 규제에 거래 위축 우려", "link": ""}]
        score = lexicon_score([n["title"] for n in news])
        fc, sig = forecast(rows, score)
        sm = smooth(rows[:-1])
        now_ix = next((sm[r["ym"]] for r in reversed(rows[:-1]) if sm.get(r["ym"])), None)
        res["regions"].append({"code": code, "name": name, "index": rows, "forecast": fc,
                               "signals": sig, "news": news[:8]})
        res["apts"] += [apt_view(a, months, sm, now_ix) for a in apts if a["code"] == code]
    res["diag"] = diag
    return res
