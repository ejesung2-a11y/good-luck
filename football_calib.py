"""
football_calib.py - 해외 축구 과거 경기 CSV(배당+결과) 업로드 → '배당 확률 vs 실제 결과' 보정표
 (예: GitHub xgabora/Club-Football-Match-Data, football-data.co.uk, FootyStats 에서 받은 CSV)
 보정표는 축구 일반(승무패) 예측의 확률을 약하게 교정하는 데 쓰입니다.
"""
import io, os, json
import numpy as np
import pandas as pd
import requests
import storage

DATA_DIR = "data"
CAL_PATH = os.path.join(DATA_DIR, "football_calib.json")
COLS = dict(
    h=["OddHome", "B365H", "AvgH", "PSH", "MaxH", "WHH", "HomeOdds"],
    d=["OddDraw", "B365D", "AvgD", "PSD", "MaxD", "WHD", "DrawOdds"],
    a=["OddAway", "B365A", "AvgA", "PSA", "MaxA", "WHA", "AwayOdds"],
    r=["FTResult", "FTR", "Result"],
)
BINS = [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0001]


def _pick(df, names):
    return next((c for c in names if c in df.columns), None)


def build_from_csv(name, raw, keep_raw=True):
    df = pd.read_csv(io.BytesIO(raw), low_memory=False)
    ch, cd, ca, cr = (_pick(df, COLS[k]) for k in "hdar")
    if not all([ch, cd, ca, cr]):
        raise ValueError(f"필요한 열을 못 찾았습니다. 배당(홈/무/원정)·결과(H/D/A) 열이 필요합니다. 찾은 열: {list(df.columns)[:25]}")
    d = df[[ch, cd, ca, cr]].dropna()
    d.columns = ["h", "d", "a", "r"]
    d = d[(d.h > 1) & (d.d > 1) & (d.a > 1) & d.r.isin(["H", "D", "A"])]
    inv = np.column_stack([1 / d.h, 1 / d.d, 1 / d.a])
    lo, hi = np.full(len(inv), 0.5), np.full(len(inv), 4.0)     # sports.devig 와 동일한 power 방식(행별 이분법)
    for _ in range(40):
        c = (lo + hi) / 2
        big = (inv ** c[:, None]).sum(axis=1) > 1
        lo, hi = np.where(big, c, lo), np.where(big, hi, c)
    pw = inv ** ((lo + hi) / 2)[:, None]
    pw = pw / pw.sum(axis=1, keepdims=True)
    p = pd.DataFrame(pw, columns=["H", "D", "A"], index=d.index)
    out = {"source": name, "n": int(len(d)), "columns": [ch, cd, ca, cr], "bins": {}}
    for k in "HDA":
        cut = pd.cut(p[k], BINS, right=False)
        grp = pd.DataFrame({"p": p[k], "y": (d.r == k).astype(float), "b": cut}).groupby("b", observed=True)
        out["bins"][k] = [dict(lo=float(i.left), hi=float(i.right), mean_p=float(g.p.mean()), actual=float(g.y.mean()), n=int(len(g)))
                          for i, g in grp]
    os.makedirs(DATA_DIR, exist_ok=True)
    if keep_raw:                                   # 업로드한 원본도 R2 에 보관
        rel = f"football/{os.path.basename(name)}"
        os.makedirs(os.path.join(DATA_DIR, "football"), exist_ok=True)
        open(os.path.join(DATA_DIR, rel), "wb").write(raw)
        storage.push(rel)
    json.dump(out, open(CAL_PATH, "w"), ensure_ascii=False)
    storage.push(os.path.relpath(CAL_PATH, DATA_DIR))
    return out


def load():
    storage.restore_once()
    if os.path.exists(CAL_PATH):
        try:
            return json.load(open(CAL_PATH))
        except Exception:
            return None
    return None


_cache = {"cal": None, "t": None}


def _cal():
    t = os.path.getmtime(CAL_PATH) if os.path.exists(CAL_PATH) else None
    if _cache["t"] != t:
        _cache["cal"], _cache["t"] = load(), t
    return _cache["cal"]


def calibrate(ph, px, pa, shrink_n=300):
    """표본이 충분한 구간만 (실제−예측) 차이를 반영. 표본 적으면 거의 변화 없음. 보정 폭은 ±4%p 로 제한."""
    cal = _cal()
    if not cal:
        return ph, px, pa, None
    res = []
    for k, p in zip("HDA", (ph, px, pa)):
        delta = 0.0
        for b in cal["bins"][k]:
            if b["lo"] <= p < b["hi"]:
                delta = (b["actual"] - b["mean_p"]) * b["n"] / (b["n"] + shrink_n)
        res.append(p + max(-0.04, min(0.04, delta)))
    s = sum(res)
    return res[0] / s, res[1] / s, res[2] / s, cal["n"]


ALLOWED_HOSTS = ("raw.githubusercontent.com", "github.com")
GITHUB_PRESET = "https://raw.githubusercontent.com/xgabora/Club-Football-Match-Data/main/data/Matches.csv"


def import_from_github(url, max_mb=300):
    """GitHub 의 CSV 를 내려받아 보정표 생성. github.com/.../blob/... 주소는 raw 주소로 자동 변환."""
    from urllib.parse import urlparse
    u = urlparse(url.strip())
    if u.scheme != "https" or u.netloc not in ALLOWED_HOSTS:
        raise ValueError("github.com 또는 raw.githubusercontent.com 의 https 주소만 허용합니다.")
    if u.netloc == "github.com":
        url = url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
    buf, size = bytearray(), 0
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(1 << 20):
            size += len(chunk)
            if size > max_mb * (1 << 20):
                raise ValueError(f"파일이 {max_mb}MB 를 넘어 중단했습니다.")
            buf += chunk
    out = build_from_csv(os.path.basename(u.path) or "github.csv", bytes(buf), keep_raw=False)   # 원본은 GitHub 에 있으므로 재보관 안 함
    out["source"] = url
    json.dump(out, open(CAL_PATH, "w"), ensure_ascii=False)
    storage.push(os.path.relpath(CAL_PATH, DATA_DIR))
    return out
