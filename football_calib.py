"""
football_calib.py - 해외 축구 과거 경기 CSV(배당+결과) 업로드 → '배당 확률 vs 실제 결과' 보정표
 (예: GitHub xgabora/Club-Football-Match-Data, football-data.co.uk, FootyStats 에서 받은 CSV)
 보정표는 축구 일반(승무패) 예측의 확률을 약하게 교정하는 데 쓰입니다.
"""
import io, os, json
import pandas as pd
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


def build_from_csv(name, raw):
    df = pd.read_csv(io.BytesIO(raw), low_memory=False)
    ch, cd, ca, cr = (_pick(df, COLS[k]) for k in "hdar")
    if not all([ch, cd, ca, cr]):
        raise ValueError(f"필요한 열을 못 찾았습니다. 배당(홈/무/원정)·결과(H/D/A) 열이 필요합니다. 찾은 열: {list(df.columns)[:25]}")
    d = df[[ch, cd, ca, cr]].dropna()
    d.columns = ["h", "d", "a", "r"]
    d = d[(d.h > 1) & (d.d > 1) & (d.a > 1) & d.r.isin(["H", "D", "A"])]
    inv = pd.DataFrame({"H": 1 / d.h, "D": 1 / d.d, "A": 1 / d.a})
    p = inv.div(inv.sum(axis=1), axis=0)
    out = {"source": name, "n": int(len(d)), "columns": [ch, cd, ca, cr], "bins": {}}
    for k in "HDA":
        cut = pd.cut(p[k], BINS, right=False)
        grp = pd.DataFrame({"p": p[k], "y": (d.r == k).astype(float), "b": cut}).groupby("b", observed=True)
        out["bins"][k] = [dict(lo=float(i.left), hi=float(i.right), mean_p=float(g.p.mean()), actual=float(g.y.mean()), n=int(len(g)))
                          for i, g in grp]
    os.makedirs(DATA_DIR, exist_ok=True)
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
