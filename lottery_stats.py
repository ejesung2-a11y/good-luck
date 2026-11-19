"""
lottery_stats.py - 로또/연금복권/파워볼: 업로드한 실제 과거 당첨번호 → 통계 → 번호 생성 → 백테스트
※ 추첨은 독립 사건이라 어떤 통계도 당첨확률을 높이지 못합니다. 이 모듈의 '백테스트'가 그 사실을 숫자로 보여줍니다.
"""
import io, os, re, json, random, sqlite3
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo
from itertools import combinations
import pandas as pd
import storage

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "lottery.db")
os.makedirs(DATA_DIR, exist_ok=True)

GAMES = {
    "lotto":     dict(name="로또 6/45", k=6, lo=1, hi=45),
    "powerball": dict(name="파워볼", k=5, lo=1, hi=28, extra=(0, 9)),
    "pension":   dict(name="연금복권720+", k=6),
}


def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    storage.restore_once()
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS draws(game TEXT, round INTEGER, nums TEXT, extra INTEGER,
                     source TEXT, PRIMARY KEY(game, round))""")


def _sync():
    storage.push(os.path.relpath(DB_PATH, DATA_DIR))


# ───────────── 업로드 파싱 (CSV / 엑셀 / 붙여넣기 텍스트) ─────────────
def _rows_from_bytes(name, raw):
    n = name.lower()
    if n.endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw), header=None, dtype=object)
        return df.values.tolist()
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            txt = raw.decode(enc)
            break
        except Exception:
            txt = None
    if txt is None:
        return []
    rows = []
    for ln in txt.splitlines():
        rows.append([c for c in re.split(r"[,\t;|]+|\s{2,}", ln.strip()) if c != ""] or re.split(r"\s+", ln.strip()))
    return rows


def _ints(row):
    """행에서 '순수 정수 칸'만 추출 (날짜/금액(쉼표)/글자 칸은 무시)"""
    out = []
    for c in row:
        if isinstance(c, (datetime, pd.Timestamp)) or c is None:
            continue
        s = str(c).strip()
        s = re.sub(r"(회|조)$", "", s)
        if re.fullmatch(r"\d{1,5}", s):
            out.append(int(s))
        elif isinstance(c, float) and c == int(c) and not pd.isna(c):
            out.append(int(c))
    return out


def parse_upload(game, name, raw):
    g = GAMES[game]
    recs = []
    for row in _rows_from_bytes(name, raw):
        if game == "pension":
            cells = [re.sub(r"(회|조)$", "", str(c).strip()) for c in row if c is not None]
            six = next((c for c in cells if re.fullmatch(r"\d{6}", c)), None)
            ints = _ints(row)
            if six and ints:
                rd = ints[0]
                grp = next((v for v in ints[1:] if 1 <= v <= 5), None)
                recs.append((rd, [int(x) for x in six], grp))
            elif len(ints) >= 8:                          # 회차, 조, d1..d6
                if all(0 <= v <= 9 for v in ints[2:8]):
                    recs.append((ints[0], ints[2:8], ints[1]))
            continue
        ints = _ints(row)
        k = g["k"]
        if game == "lotto" and len(ints) >= k + 2:        # 회차, n1..n6, 보너스
            rd, nums = ints[0], ints[1:1 + k]
            if all(1 <= v <= 45 for v in nums) and len(set(nums)) == k:
                recs.append((rd, sorted(nums), ints[1 + k] if 1 <= ints[1 + k] <= 45 else None))
        elif game == "powerball" and len(ints) >= k + 2:  # 회차, 5개, 파워볼
            rd, nums = ints[0], ints[1:1 + k]
            if all(1 <= v <= 28 for v in nums) and len(set(nums)) == k and 0 <= ints[1 + k] <= 9:
                recs.append((rd, sorted(nums), ints[1 + k]))
    return recs


def save_draws(game, recs, source):
    init_db()
    with _conn() as c:
        for rd, nums, extra in recs:
            c.execute("INSERT OR REPLACE INTO draws VALUES(?,?,?,?,?)", (game, int(rd), json.dumps(nums), extra, source))
    _sync()
    return len(recs)


def load_draws(game):
    init_db()
    with _conn() as c:
        rows = c.execute("SELECT round, nums, extra FROM draws WHERE game=? ORDER BY round", (game,)).fetchall()
    return [dict(round=r, nums=json.loads(n), extra=e) for r, n, e in rows]


def clear_game(game):
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM draws WHERE game=?", (game,))
    _sync()


def save_raw_upload(game, name, raw):
    rel = f"uploads/{game}/{datetime.now(ZoneInfo('Asia/Seoul')):%Y%m%d_%H%M%S}_{os.path.basename(name)}"
    p = os.path.join(DATA_DIR, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(raw)
    storage.push(rel)


# ───────────── 통계 ─────────────
def stats(game, draws, recent=30):
    g = GAMES[game]
    if game == "pension":
        pos = [Counter() for _ in range(6)]
        for d in draws:
            for i, v in enumerate(d["nums"]):
                pos[i][v] += 1
        return dict(n=len(draws), pos=pos)
    lo, hi = g["lo"], g["hi"]
    freq_all, freq_rec = Counter(), Counter()
    last_seen = {}
    pairs = Counter()
    for idx, d in enumerate(draws):
        freq_all.update(d["nums"])
        if idx >= len(draws) - recent:
            freq_rec.update(d["nums"])
        for v in d["nums"]:
            last_seen[v] = idx
        pairs.update(combinations(d["nums"], 2))
    gap = {v: (len(draws) - 1 - last_seen[v]) if v in last_seen else len(draws) for v in range(lo, hi + 1)}
    sums = sorted(sum(d["nums"]) for d in draws) or [0]
    odds = Counter(sum(1 for v in d["nums"] if v % 2) for d in draws)
    return dict(n=len(draws), freq_all=freq_all, freq_rec=freq_rec, gap=gap, pairs=pairs,
                sum_lo=sums[int(len(sums) * 0.1)], sum_hi=sums[int(len(sums) * 0.9)],
                odd_range=[k for k, c in odds.items() if c >= max(1, 0.05 * len(draws))] or [2, 3, 4], odds=odds)


def _weights(game, st, mode):
    g = GAMES[game]
    w = {}
    n = max(st["n"], 1)
    for v in range(g["lo"], g["hi"] + 1):
        base = 1.0
        if mode == "hot":
            base += 3.0 * st["freq_rec"].get(v, 0) / max(1, max(st["freq_rec"].values() or [1]))
        elif mode == "cold":
            base += 3.0 * min(st["gap"][v], 40) / 40
        elif mode == "balanced":
            exp = n * g["k"] / (g["hi"] - g["lo"] + 1)
            base += 0.5 * min(max((exp - st["freq_all"].get(v, 0)) / max(exp, 1), -1), 1)
        w[v] = max(base, 0.05)
    return w


def generate(game, draws, mode="balanced", fixed=(), exclude=(), rng=None, strict=True):
    rng = rng or random
    g = GAMES[game]
    if game == "pension":
        return generate_pension(draws, rng)
    st = stats(game, draws, 30) if draws else None
    pool = [v for v in range(g["lo"], g["hi"] + 1) if v not in exclude and v not in fixed]
    w = _weights(game, st, mode) if st else {v: 1.0 for v in pool}
    need = g["k"] - len(fixed)
    best = None
    for _ in range(4000):
        picked = set()
        while len(picked) < need:
            picked.add(rng.choices(pool, weights=[w[v] for v in pool], k=1)[0])
        nums = sorted(list(fixed) + list(picked))
        if st and strict:
            odd = sum(v % 2 for v in nums)
            ok = st["sum_lo"] <= sum(nums) <= st["sum_hi"] and odd in st["odd_range"]
            if game == "lotto":
                ok = ok and len({(v - 1) // 10 for v in nums}) >= 3
            if not ok:
                best = best or nums
                continue
        best = nums
        break
    nums = best
    extra = None
    if game == "powerball":
        extra = rng.randint(*g["extra"])
    why = f"합계 {sum(nums)} · 홀수 {sum(v % 2 for v in nums)}개" + (f" · 번호구간 {len({(v-1)//10 for v in nums})}개" if game == "lotto" else "")
    return dict(nums=nums, extra=extra, why=why + (f" · 학습 {st['n']}회차" if st else " · 학습데이터 없음(무작위)"))


def generate_pension(draws, rng, group="random"):
    st = stats("pension", draws) if draws else None
    digits = []
    for i in range(6):
        w = [1.0 + (st["pos"][i].get(d, 0) / max(1, st["n"]) * 2 if st else 0) for d in range(10)]
        digits.append(rng.choices(range(10), weights=w, k=1)[0])
    return dict(nums=digits, extra=rng.randint(1, 5), why=f"자리별 출현비율 약하게 반영 · 합계 {sum(digits)}" + (f" · 학습 {st['n']}회차" if st else " · 학습데이터 없음"))


# ───────────── 백테스트: 이 방법이 무작위보다 나은가? ─────────────
def backtest(game, draws, mode, test_draws=200, games_per_draw=5, seed=7):
    """각 회차 직전까지의 데이터로 번호를 만들고 실제 당첨번호와 비교(걸러지는 미래정보 없음)"""
    rng = random.Random(seed)
    n = len(draws)
    start = max(30, n - test_draws)
    if n - start < 20:
        return None
    g = GAMES[game]
    diffs, my_all, rnd_all = [], [], []
    for t in range(start, n):
        hist, truth = draws[:t], draws[t]["nums"]
        my, rnd = [], []
        for _ in range(games_per_draw):
            if game == "pension":
                a = generate_pension(hist, rng)["nums"]
                b = [rng.randint(0, 9) for _ in range(6)]
                m = lambda x: next((i for i in range(6) if x[5 - i] != truth[5 - i]), 6)   # 뒤에서부터 연속 일치 자릿수
                my.append(m(a)); rnd.append(m(b))
            else:
                a = generate(game, hist, mode, rng=rng)["nums"]
                b = sorted(rng.sample(range(g["lo"], g["hi"] + 1), g["k"]))
                my.append(len(set(a) & set(truth))); rnd.append(len(set(b) & set(truth)))
        # 같은 회차의 게임들은 서로 비슷하므로 '회차 단위'로 묶어서 비교해야 과대평가를 피한다
        my_all.append(sum(my) / len(my)); rnd_all.append(sum(rnd) / len(rnd))
        diffs.append(my_all[-1] - rnd_all[-1])
    N = len(diffs)
    md = sum(diffs) / N
    sd = (sum((d - md) ** 2 for d in diffs) / max(1, N - 1)) ** 0.5 or 1e-9
    return dict(rounds=N, games=N * games_per_draw, my_mean=sum(my_all) / N, rnd_mean=sum(rnd_all) / N, z=md / (sd / N ** 0.5))
