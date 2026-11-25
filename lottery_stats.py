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
    os.makedirs(DATA_DIR, exist_ok=True)
    storage.restore_once()
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS draws(game TEXT, round INTEGER, nums TEXT, extra INTEGER,
                     source TEXT, PRIMARY KEY(game, round))""")


def _sync():
    storage.push(os.path.relpath(DB_PATH, DATA_DIR))


# ───────────── 업로드 파싱 (CSV / 엑셀 / 붙여넣기 텍스트) ─────────────
def _pdf_text(raw):
    """인쇄(PDF 저장)한 파일의 글자. 글자 레이어가 없는 이미지형이면 OCR(부정확할 수 있음)."""
    txt = ""
    try:                                                # pdfium 이 pypdf 보다 훨씬 빠름
        import pypdfium2 as pdfium
        d = pdfium.PdfDocument(raw)
        txt = "\n".join(d[i].get_textpage().get_text_range() for i in range(len(d)))
    except Exception:
        pass
    if len(re.findall(r"\d", txt)) < 30:
        try:
            from pypdf import PdfReader
            txt = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
        except Exception:
            pass
    txt = re.sub(r"[\x00-\x08\x0b-\x1f\u00a0]", " ", txt)
    if len(re.findall(r"\d", txt)) >= 30:
        return txt, "PDF 텍스트"
    import sports                                       # 이미지형 PDF → OCR
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(raw)
    return sports._ocr_pages([pdf[i].render(scale=4).to_pil() for i in range(len(pdf))]), "OCR(이미지 PDF)"


def _pdf_row_sets(raw):
    """두 가지 방식으로 행을 만든다: (1) 줄 단위 (2) '1100회' 표시로 나눈 블록 단위(한 회차 정보가 여러 줄로 쪼개진 경우)"""
    txt, method = _pdf_text(raw)
    lines = [re.split(r"\s+", l.strip()) for l in txt.splitlines() if l.strip()]
    marks = list(re.finditer(r"(\d{1,4})\s*회(?!\s*차)", txt))
    blocks = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(txt)
        seg = txt[m.start():end]
        blocks.append([m.group(1)] + re.split(r"\s+", re.sub(r"^\d{1,4}\s*회", "", seg).strip()))
    return lines, blocks, method


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


def _find_window(game, ints):
    """행의 정수열에서 '오름차순 당첨번호 k개 + 보너스/파워볼' 구간을 찾는다.
    당첨자 수·금액 같은 다른 숫자 칸이 섞여 있어도(동행복권 엑셀) 인식하도록 구간 탐색.
    회차 = 구간 앞쪽의 첫 정수."""
    g = GAMES[game]
    k, hi = g["k"], g["hi"]
    for i in range(1, len(ints) - k):
        nums, ex = ints[i:i + k], ints[i + k]
        if all(1 <= v <= hi for v in nums) and all(a < b for a, b in zip(nums, nums[1:])):
            if game == "lotto" and 1 <= ex <= 45 and ex not in nums:
                return ints[0], nums, ex
            if game == "powerball" and 0 <= ex <= 9:
                return ints[0], nums, ex
    return None


def parse_upload(game, name, raw):
    if name.lower().endswith(".pdf"):
        lines, blocks, _ = _pdf_row_sets(raw)
        a, b = _parse_rows(game, lines), _parse_rows(game, blocks)
        best = a if len(a) >= len(b) else b
        return list({r[0]: r for r in best}.values())       # 같은 회차 중복 제거
    return _parse_rows(game, _rows_from_bytes(name, raw))


def _parse_rows(game, rows):
    g = GAMES[game]
    recs = []
    for row in rows:
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
        w = _find_window(game, ints)
        if w:
            recs.append(w)
    return recs


def save_draws(game, recs, source):
    init_db()
    with _conn() as c:
        for rd, nums, extra in recs:
            c.execute("INSERT OR REPLACE INTO draws VALUES(?,?,?,?,?)", (game, int(rd), json.dumps(nums), extra, source))
    _sync()
    return len(recs)


def summary(draws):
    """업로드 점검용: 회차 범위와 누락 회차"""
    if not draws:
        return None
    rounds = [d["round"] for d in draws]
    miss = sorted(set(range(min(rounds), max(rounds) + 1)) - set(rounds))
    return dict(n=len(rounds), first=min(rounds), last=max(rounds), missing=miss)


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


def rebuild_from_storage():
    """DB 가 비었을 때: R2 의 uploads/<게임>/ 원본에서 당첨번호를 다시 읽어 복구(오래된 순으로 읽어 최신이 덮어씀)."""
    init_db()
    total = {}
    for game in GAMES:
        items = []
        for rel, _ in storage.list_keys():
            m = re.fullmatch(rf"uploads/{game}/(\d{{8}}_\d{{6}})_(.+)", rel)
            if m:
                items.append((m.group(1), m.group(2), rel))
        n = 0
        for _, name, rel in sorted(items):
            path = storage.fetch(rel)
            if path:
                recs = parse_upload(game, name, open(path, "rb").read())
                n += save_draws(game, recs, name) if recs else 0
        total[game] = n
    return total


# ───────────── 직접 입력 (회차 + 당첨번호) ─────────────
def parse_numbers(text):
    return [int(x) for x in re.findall(r"\d+", str(text or ""))]


def validate_draw(game, rd, nums, extra):
    """입력값 검사. return 오류 문구 리스트(비어 있으면 정상)"""
    errs = []
    g = GAMES[game]
    if rd is None or int(rd) < 1:
        errs.append("회차는 1 이상이어야 합니다.")
    if game == "pension":
        if len(nums) != 6 or any(not 0 <= v <= 9 for v in nums):
            errs.append("당첨번호는 0~9 숫자 6자리여야 합니다. (예: 482915)")
        if extra is None or not 1 <= int(extra) <= 5:
            errs.append("조는 1~5 중 하나여야 합니다.")
        return errs
    if len(nums) != g["k"] or len(set(nums)) != g["k"] or any(not g["lo"] <= v <= g["hi"] for v in nums):
        errs.append(f"당첨번호는 서로 다른 {g['k']}개 숫자({g['lo']}~{g['hi']})여야 합니다.")
    if game == "lotto":
        if extra is None or not 1 <= int(extra) <= 45 or (extra in nums):
            errs.append("보너스 번호는 1~45 이고 당첨번호와 겹치면 안 됩니다.")
    if game == "powerball":
        if extra is None or not 0 <= int(extra) <= 9:
            errs.append("파워볼은 0~9 중 하나여야 합니다.")
    return errs


def add_draw(game, rd, nums, extra, source="직접입력"):
    """회차 1건 저장(같은 회차가 있으면 덮어씀). return (오류 리스트, 덮어썼는지)"""
    errs = validate_draw(game, rd, nums, extra)
    if errs:
        return errs, False
    init_db()
    with _conn() as c:
        existed = c.execute("SELECT 1 FROM draws WHERE game=? AND round=?", (game, int(rd))).fetchone() is not None
    nums = list(nums) if game == "pension" else sorted(nums)
    save_draws(game, [(int(rd), nums, int(extra))], source)
    return [], existed


def delete_draw(game, rd):
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM draws WHERE game=? AND round=?", (game, int(rd)))
    _sync()


def reset_game(game, delete_originals=False):
    """한 게임의 저장된 회차를 모두 지우고(R2 의 DB 에도 반영), 선택하면 업로드 원본도 삭제"""
    init_db()
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM draws WHERE game=?", (game,)).fetchone()[0]
        c.execute("DELETE FROM draws WHERE game=?", (game,))
    removed = 0
    if delete_originals:
        d = os.path.join(DATA_DIR, "uploads", game)
        if os.path.isdir(d):
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
        rels = [rel for rel, _ in storage.list_keys() if rel.startswith(f"uploads/{game}/")] if storage.enabled() else []
        removed = storage.remove_many(rels)
    _sync()
    return dict(draws=n, originals=removed)
