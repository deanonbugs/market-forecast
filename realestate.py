# -*- coding: utf-8 -*-
"""부동산: 국토부 아파트 매매 실거래가(상세) → 지역 흐름·단지 시세 추정·전망·검색용 데이터"""
import csv
import json
import math
import random
import statistics as st
import time
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = BASE / "realestate_cache.json"      # 예전 형식 (있으면 옮겨오고 지움)
CACHE_DIR = BASE / "re_cache"               # 지역별 원자료 (GitHub Actions 캐시에 보관)
API = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
MONTHS = 36        # 모아둘 기간 (개월)
REFRESH = 3        # 하루 한 번 다시 받는 최근 개월 수 (늦게 신고되는 거래 반영)
FILL_BUDGET = 350  # 한 번 실행할 때 빈 달을 채우는 최대 호출 수 (하루 호출 한도 보호)
HZ = {"m3": 3, "m6": 6, "m12": 12}
W = {"news": .25, "trend": .30, "mom": .25, "volume": .20}
TILT = 0.2
F = {"apt": ("aptNm", "아파트"), "dong": ("umdNm", "법정동"), "area": ("excluUseAr", "전용면적"),
     "amt": ("dealAmount", "거래금액"), "y": ("dealYear", "년"), "m": ("dealMonth", "월"),
     "d": ("dealDay", "일"), "floor": ("floor", "층"), "built": ("buildYear", "건축년도"),
     "cancel": ("cdealType", "해제여부")}


def _csv(name):
    p = BASE / name
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if r and not r[0].lstrip().startswith("#")]
    return [[x.strip() for x in r] + [""] * 6 for r in rows[1:]]


def read_apts():
    return [{"name": r[0], "code": r[1], "region": r[2] or r[1], "dong": r[3], "kw": r[4] or r[0], "area": r[5]}
            for r in _csv("apartments.csv") if r[0] and r[1]]


def read_regions():
    return [{"code": r[0], "name": r[1] or r[0], "group": r[2]} for r in _csv("regions.csv") if r[0]]


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
        for attempt in range(3):  # 서버가 느릴 때를 대비해 3번까지 다시 시도
            try:
                r = requests.get(API, timeout=40, params={"serviceKey": key, "LAWD_CD": code,
                                                          "DEAL_YMD": ym, "pageNo": page, "numOfRows": 1000})
                break
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))
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
            items.append([_get(it, "apt"), _get(it, "dong"), area, amt, d, _get(it, "floor"), _get(it, "built")])
        total = int((root.findtext(".//totalCount") or "0").strip() or 0)
        if page * 1000 >= total:
            return items
        page += 1


def _load_region(code):
    try:
        return json.loads((CACHE_DIR / f"{code}.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    try:  # 예전 형식에서 옮겨오기
        old = json.loads(CACHE.read_text(encoding="utf-8")).get(code, {})
        return {ym: [[t["apt"], t["dong"], t["area"], t["amt"], t["date"], t["floor"], t["built"]] for t in v]
                for ym, v in old.items()}
    except Exception:
        return {}


def collect(key, codes, priority, today, diag):
    """최근 3개월은 하루 한 번 갱신, 빈 달은 예산 안에서 최근 달부터 채움"""
    CACHE_DIR.mkdir(exist_ok=True)
    yms = months_back(MONTHS, today)
    data = {c: _load_region(c) for c in codes}
    order = [c for c in priority if c in data] + [c for c in codes if c not in priority]
    calls, stop, fails = 0, None, 0

    def get(code, ym):
        nonlocal calls, stop, fails
        if stop:
            return False
        try:
            data[code][ym] = fetch_month(key, code, ym)
            calls += 1
            fails = 0
            return True
        except Exception as e:
            msg = str(e)[:160]
            if msg.startswith(("API 오류", "응답 해석")):
                stop = msg          # 한도 초과·키 문제는 바로 멈춤
            else:
                fails += 1          # 일시적인 연결 문제는 그 달만 건너뛰고 계속
                if fails >= 8:
                    stop = "연결 문제가 계속돼서 이번엔 여기까지 받았어요"
            return False

    for code in order:
        if data[code].get("_refreshed") != today.isoformat():
            if all([get(code, ym) for ym in yms[-REFRESH:]]):
                data[code]["_refreshed"] = today.isoformat()
    filled = 0
    for ym in reversed(yms[:-REFRESH]):
        for code in order:
            if filled >= FILL_BUDGET or stop:
                break
            if ym not in data[code] and get(code, ym):
                filled += 1
    for code, c in data.items():
        for k in [k for k in c if not k.startswith("_") and k not in yms]:
            del c[k]
        (CACHE_DIR / f"{code}.json").write_text(json.dumps(c, ensure_ascii=False, separators=(",", ":")),
                                                encoding="utf-8")
    if CACHE.exists():
        CACHE.unlink()
    diag.append(f"호출 {calls}회" + (f" · 중단: {stop}" if stop else ""))
    return {code: {ym: [{"apt": r[0], "dong": r[1], "area": r[2], "amt": r[3], "date": r[4], "floor": r[5],
                         "built": r[6]} for r in rows] for ym, rows in c.items() if not ym.startswith("_")}
            for code, c in data.items()}, stop


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


def area_view(xs, sm, now_ix):
    adj = [t["amt"] * now_ix / sm[ym_of(t["date"])] for t in xs[-5:] if now_ix and sm.get(ym_of(t["date"]))]
    ser = {}
    for t in xs:
        ser.setdefault(ym_of(t["date"]), []).append(t["amt"])
    return {"area": round(xs[0]["area"]), "n": len(xs), "est": round(st.median(adj)) if adj else xs[-1]["amt"],
            "last": xs[-1], "series": [{"ym": ym, "p": round(st.median(v)), "n": len(v)} for ym, v in sorted(ser.items())]}


def apt_view(a, months, sm, now_ix):
    allt = [t for ym in sorted(months) for t in months[ym]]
    in_dong = [t for t in allt if not a["dong"] or a["dong"] in t["dong"]]
    trades = [t for t in in_dong if a["kw"] in t["apt"]]
    if a["area"]:
        try:
            trades = [t for t in trades if abs(t["area"] - float(a["area"])) < 3]
        except ValueError:
            pass
    trades.sort(key=lambda t: t["date"])
    groups = {}
    for t in trades:
        groups.setdefault(round(t["area"]), []).append(t)
    return {"name": a["name"], "code": a["code"], "region": a["region"], "dong": a["dong"], "kw": a["kw"],
            "built": next((t["built"] for t in trades if t["built"]), ""),
            "areas": [area_view(xs, sm, now_ix) for _, xs in sorted(groups.items())],
            "trades": [dict(t, area=round(t["area"], 2)) for t in trades[-30:][::-1]],
            "dong_names": [] if trades else sorted({t["apt"] for t in in_dong})[:30]}


def complexes(months, sm, now_ix):
    groups = {}
    for ym in sorted(months):
        for t in months[ym]:
            groups.setdefault((t["apt"], t["dong"]), []).append(t)
    out = []
    for (apt, dong), ts in groups.items():
        ts.sort(key=lambda t: t["date"])
        byar = {}
        for t in ts:
            byar.setdefault(round(t["area"]), []).append(t)
        ars = []
        for _, xs in sorted(byar.items()):
            v = area_view(xs, sm, now_ix)
            ars.append({"area": v["area"], "est": v["est"], "n": v["n"],
                        "tr": [[t["date"], t["amt"], t["floor"]] for t in xs[-6:][::-1]],
                        "s": [[x["ym"], x["p"], x["n"]] for x in v["series"]]})
        out.append({"a": apt, "d": dong, "b": next((t["built"] for t in ts if t["built"]), ""), "n": len(ts), "ar": ars})
    out.sort(key=lambda c: -c["n"])
    return out


def demo_months(code, today):
    rnd, out, lvl = random.Random(code), {}, random.Random(code).uniform(3000, 8000)
    names = (["대청", "개포자이", "래미안블레스티지", "은마"] if code == "11680"
             else [f"{w}{k}" for w in ("래미안", "자이", "푸르지오", "힐스테이트") for k in ("1단지", "2차", "센트럴")])
    for ym in months_back(MONTHS, today):
        lvl *= math.exp(rnd.gauss(0.004, 0.012))
        out[ym] = [{"apt": (nm := rnd.choice(names)), "dong": "개포동" if code == "11680" else rnd.choice(["가동", "나동"]),
                    "area": (ar := rnd.choice([39.5, 59.9, 84.9, 114.2])),
                    "amt": int(lvl * ar * rnd.uniform(.85, 1.15) / 100) * 100,
                    "date": f"{ym[:4]}-{ym[4:]}-{rnd.randint(1, 28):02d}", "floor": str(rnd.randint(1, 15)),
                    "built": "1992" if nm == "대청" else "2015"} for _ in range(rnd.randint(80, 200))]
    return out


def build(key, fetch_news, lexicon_score, today, out_dir, demo=False):
    apts, regions = read_apts(), read_regions()
    if not apts and not regions:
        return None
    names = {r["code"]: r for r in regions}
    for a in apts:
        names.setdefault(a["code"], {"code": a["code"], "name": a["region"], "group": ""})
    codes = list(names)
    prio = [a["code"] for a in apts]
    diag, res = [], {"updated": today.isoformat(), "regions": [], "apts": [], "error": "", "catalog": [], "summ": []}
    if demo:
        months_all, stop = {c: demo_months(c, today) for c in codes[:4] + prio}, None
    elif not key:
        res["error"] = "GitHub 비밀값 DATA_GO_KR_KEY가 없어요."
        return res
    else:
        try:
            months_all, stop = collect(key, codes, prio, today, diag)
        except Exception as e:
            res["error"], res["diag"] = str(e)[:200], diag
            return res
        if stop:
            res["error"] = f"일부만 받았어요 (다음 실행 때 이어서 받아요): {stop}"
    od = Path(out_dir) / "re"
    od.mkdir(parents=True, exist_ok=True)
    index = []
    for code in codes:
        months = months_all.get(code) or {}
        info = names[code]
        rows = region_index(months)
        news = []
        if code in prio:
            queries = [f"{info['name']} 아파트값", f"{info['name']} 부동산"] + [f"{a['dong']} {a['kw']}" for a in apts if a["code"] == code]
            seen = set()
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
                news = [{"title": f"{info['name']} 아파트값 상승폭 확대", "link": ""}, {"title": "대출 규제에 거래 위축 우려", "link": ""}]
        fc, sig = forecast(rows, lexicon_score([n["title"] for n in news]))
        sm = smooth(rows[:-1])
        now_ix = next((sm[r["ym"]] for r in reversed(rows[:-1]) if sm.get(r["ym"])), None)
        filled = sum(1 for r in rows if r["n"])
        res["catalog"].append({"code": code, "name": info["name"], "group": info["group"], "months": filled})
        if not months:
            continue
        last = next((r for r in reversed(rows) if r["ppa"]), None)
        res["summ"].append({"code": code, "name": info["name"], "group": info["group"], "fc": fc,
                            "ret12": sig.get("ret12"), "ppa": last["ppa"] if last else None})
        cx = complexes(months, sm, now_ix)
        index += [[c["a"], c["d"], code, c["b"], c["n"]] for c in cx]
        (od / f"{code}.json").write_text(json.dumps({"code": code, "name": info["name"], "fc": fc, "sig": sig,
                                                     "idx": rows, "cx": cx}, ensure_ascii=False,
                                                    separators=(",", ":")), encoding="utf-8")
        if code in prio:
            res["regions"].append({"code": code, "name": info["name"], "index": rows, "forecast": fc,
                                   "signals": sig, "news": news[:8]})
            res["apts"] += [apt_view(a, months, sm, now_ix) for a in apts if a["code"] == code]
    index.sort(key=lambda r: -r[4])
    (od / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    res["diag"] = diag
    return res
