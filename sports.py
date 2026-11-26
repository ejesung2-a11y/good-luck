"""
sports.py  -  와이즈토토(프로토 승부식) 경기전 예측 엔진
 - 입력: PDF(텍스트형/이미지형) 또는 PNG/JPG 캡처  ->  OCR -> 행 파싱 -> (사용자 검수) -> DB 누적 저장
 - 예측: 배당 마진 제거(진확률) + 포아송/정규분포 점수 모델 + 누적 학습 보정(팀 폼)
 - 모든 시각은 한국시간(KST) 기준
"""
import io, os, re, math, json, sqlite3, hashlib, zipfile
from collections import Counter
from datetime import datetime, timedelta
from statistics import NormalDist
from zoneinfo import ZoneInfo

import pandas as pd
import storage
import football_calib

KST = ZoneInfo("Asia/Seoul")
DATA_DIR = "data"
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "sports_quantum.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def now_kst():
    return datetime.now(KST).replace(tzinfo=None)


# ───────────────────────── DB ─────────────────────────
def _conn():
    return sqlite3.connect(DB_PATH)


def _rel(p):
    return os.path.relpath(p, DATA_DIR)


def _sync_db():
    storage.push(_rel(DB_PATH))


def init_sports_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    storage.restore_once()
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS uploads(
            source_file TEXT PRIMARY KEY, round_label TEXT, file_hash TEXT,
            uploaded_at TEXT, saved_path TEXT, row_count INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS matches(
            row_key TEXT PRIMARY KEY, source_file TEXT, round_label TEXT, game_no TEXT,
            match_datetime TEXT, league TEXT, sport TEXT, bet_type TEXT, line REAL,
            home_team TEXT, away_team TEXT, odds1 REAL, oddsx REAL, odds2 REAL,
            home_score INTEGER, away_score INTEGER, uploaded_at TEXT, total_score REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS odds_history(
            row_key TEXT, snap_at TEXT, odds1 REAL, oddsx REAL, odds2 REAL, source_file TEXT)""")
        if "old_line" not in [r[1] for r in c.execute("PRAGMA table_info(odds_history)")]:
            c.execute("ALTER TABLE odds_history ADD COLUMN old_line REAL")
        c.execute("CREATE INDEX IF NOT EXISTS ix_oh ON odds_history(row_key)")
        c.execute("""CREATE TABLE IF NOT EXISTS pred_log(
            row_key TEXT PRIMARY KEY, logged_at TEXT, bet_type TEXT, sport TEXT, pred_pick TEXT,
            base_prob REAL, pred_prob REAL, pred_label TEXT, odds1 REAL, oddsx REAL, odds2 REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS game_notes(game_key TEXT PRIMARY KEY, memo TEXT, updated_at TEXT)""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(matches)")]
        for col, typ in (("total_score", "REAL"), ("trend1", "INTEGER"), ("trendx", "INTEGER"), ("trend2", "INTEGER")):
            if col not in cols:
                c.execute(f"ALTER TABLE matches ADD COLUMN {col} {typ}")


def data_version():
    """데이터 버전(경기 행수, 마지막 저장시각, 파일 수). 파일 시각 대신 DB 를 직접 조회해 캐시가 낡은 채 남지 않게 한다."""
    init_sports_db()
    with _conn() as c:
        return c.execute("SELECT COUNT(*), COALESCE(MAX(uploaded_at), ''), (SELECT COUNT(*) FROM uploads) FROM matches").fetchone()


def known_teams():
    init_sports_db()
    with _conn() as c:
        rows = c.execute("SELECT home_team FROM matches UNION SELECT away_team FROM matches").fetchall()
    return {r[0] for r in rows if r[0]}


def snap_team_names(df, known, cutoff=0.6):
    """OCR 로 깨진 팀명을 이미 저장된(정확히 읽힌) 팀명 중 가장 비슷한 것으로 맞춘다."""
    import difflib
    if df.empty or not known:
        return df, 0
    out, cache, n = df.copy(), {}, 0
    pool = list(known)
    for col in ("home_team", "away_team"):
        vals = []
        for v in out[col]:
            if not v or v in known:
                vals.append(v)
                continue
            if v not in cache:
                m = difflib.get_close_matches(v, pool, n=1, cutoff=cutoff)
                cache[v] = m[0] if m else v
            n += cache[v] != v
            vals.append(cache[v])
        out[col] = vals
    return out, n


def review_flags(df, known=None):
    """저장 전 사람이 확인해야 할 행 표시 ([확인필요])"""
    flags = []
    for r in df.to_dict("records"):
        f = []
        if r["bet_type"] != "SUM" and pd.notna(r["odds1"]) and not odds_ok(r):
            f.append("배당이상")
        if r["bet_type"] in ("H", "U", "hH", "hU") and pd.isna(r["line"]):
            f.append("기준값없음")
        if not r["home_team"] or not r["away_team"]:
            f.append("팀명없음")
        elif known is not None and known and (r["home_team"] not in known or r["away_team"] not in known):
            f.append("새팀명?")
        flags.append(" · ".join(f))
    return flags


def get_uploaded_files():
    with _conn() as c:
        return pd.read_sql("""SELECT u.source_file AS 파일명, u.round_label AS 회차, u.row_count AS 행수,
            substr(u.uploaded_at,1,19) AS 업로드시각 FROM uploads u ORDER BY u.uploaded_at DESC""", c)


def delete_file_from_db(source_file):
    with _conn() as c:
        row = c.execute("SELECT saved_path FROM uploads WHERE source_file=?", (source_file,)).fetchone()
        c.execute("DELETE FROM matches WHERE source_file=?", (source_file,))
        c.execute("DELETE FROM uploads WHERE source_file=?", (source_file,))
    if row and row[0] and os.path.exists(row[0]):
        os.remove(row[0])
        storage.remove(_rel(row[0]))
    _sync_db()


ORIGINAL_RE = re.compile(r"uploads/\d{8}_\d{6}_[^/]+\.(pdf|png|jpe?g|txt)", re.I)


def sports_originals():
    """토토 업로드 원본 목록. return (R2 쪽 rel 목록, 이 서버 로컬 파일 목록)"""
    r2 = [rel for rel, _ in storage.list_keys() if ORIGINAL_RE.fullmatch(rel)] if storage.enabled() else []
    loc = []
    if os.path.isdir(UPLOAD_DIR):
        loc = [os.path.join(UPLOAD_DIR, f) for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f))]
    return r2, loc


def reset_all(delete_originals=False):
    """토토 데이터 초기화. DB(경기·이력·예측기록·메모)를 비우고 비워진 DB 를 R2 에도 올려 이전 DB 가 되살아나지 않게 한다.
    delete_originals=True 이면 업로드 원본(PDF·이미지·텍스트)과 master_games.csv 도 로컬·R2 에서 삭제(완전 초기화).
    return 처리 결과 dict"""
    init_sports_db()
    with _conn() as c:
        rows = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        files = c.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
        for t in ("matches", "uploads", "odds_history", "pred_log", "game_notes"):
            c.execute(f"DELETE FROM {t}")
    out = dict(rows=rows, files=files, r2_originals=0, local_originals=0)
    if delete_originals:
        r2, loc = sports_originals()
        for f in loc:
            try:
                os.remove(f)
            except OSError:
                pass
        mp = os.path.join(DATA_DIR, "master", "master_games.csv")
        extra = []
        if os.path.exists(mp):
            os.remove(mp)
        if storage.enabled():
            extra = ["master/master_games.csv"] if "master/master_games.csv" in {k for k, _ in storage.list_keys()} else []
        out["r2_originals"] = storage.remove_many(r2 + extra)
        out["local_originals"] = len(loc)
    _sync_db()                                  # 비워진 DB 를 R2 에 반영
    return out


def clear_sports_db():
    with _conn() as c:
        c.execute("DELETE FROM matches"); c.execute("DELETE FROM uploads")
        c.execute("DELETE FROM odds_history"); c.execute("DELETE FROM pred_log")
    _sync_db()


def load_matches():
    """저장된 경기(경기당 최신 값만) + 배당 변경 이력(처음 배당 open*, 직전 배당 prev*, 변경 횟수 n_chg)"""
    with _conn() as c:
        df = pd.read_sql("SELECT * FROM matches ORDER BY match_datetime, game_no", c)
        h = pd.read_sql("SELECT row_key, snap_at, odds1, oddsx, odds2, old_line FROM odds_history ORDER BY snap_at", c)
    for col in ("open1", "openx", "open2", "prev1", "prevx", "prev2", "open_line"):
        df[col] = float("nan")
    df["n_chg"] = 0
    if not h.empty and not df.empty:
        g = h.groupby("row_key")
        first, last, cnt = g.first(), g.last(), g.size()
        m = df.row_key
        for src, dst in (("odds1", "open1"), ("oddsx", "openx"), ("odds2", "open2")):
            df[dst] = m.map(first[src])
        for src, dst in (("odds1", "prev1"), ("oddsx", "prevx"), ("odds2", "prev2")):
            df[dst] = m.map(last[src])
        df["open_line"] = m.map(first["old_line"])
        df["n_chg"] = m.map(cnt).fillna(0).astype(int)
    return df


def get_matches_by_file(source_file):
    with _conn() as c:
        return pd.read_sql("""SELECT game_no, match_datetime, league, sport, bet_type, line, home_team, away_team,
            odds1, oddsx, odds2, home_score, away_score FROM matches WHERE source_file=?
            ORDER BY match_datetime, game_no""", c, params=(source_file,))


def stash_raw(source_file, raw_bytes):
    """읽기(파싱) 시점에 원본을 바로 저장·R2 업로드 → 검수 전에 창을 닫아도 원본은 남는다. 저장 경로 반환."""
    stamp = now_kst().strftime("%Y%m%d_%H%M%S")
    p = os.path.join(UPLOAD_DIR, f"{stamp}_{os.path.basename(source_file)}")
    with open(p, "wb") as f:
        f.write(raw_bytes)
    import threading
    threading.Thread(target=storage.push, args=(_rel(p),), daemon=True).start()   # R2 업로드는 뒤에서(화면이 기다리지 않게)
    return p


def _round_ok(new_label, old_label):
    """새 파일의 회차가 기존 줄의 회차와 같거나 더 최신이면 True. (이전 회차 파일이 더 최신 회차의 줄을 덮어쓰지 못하게)
    연도가 둘 다 있으면 (연도, 회차) 순, 아니면 회차 번호만 비교한다."""
    def parse(lb):
        m = re.match(r"(?:(\d{4})년\s*)?(\d+)회차", str(lb or ""))
        return (int(m.group(1)) if m and m.group(1) else None, int(m.group(2)) if m else None)
    (ny, nn), (oy, on) = parse(new_label), parse(old_label)
    if nn is None or on is None:
        return True
    if ny and oy and ny != oy:
        return ny > oy
    return nn >= on


def save_matches(df, source_file, round_label, raw_bytes=None, saved_path=None, sync=True):
    """저장 규칙(같은 경기 = 시각+리그+유형+기준값+팀):
    ① 아직 시작 안 한 경기는 '가장 최근 회차'의 최신 업로드로 덮어쓴다(배당이 바뀌면 이력 보관).
    ② 결과가 확정된 경기·이미 시작한 경기·더 최신 회차에 이미 있는 경기는 덮어쓰지 않고 참고·학습용으로 보존한다.
       (단, 이미 시작한 경기에 결과가 담긴 파일이 오면 결과를 채운다)
    return 처리 건수 dict"""
    stats = {"신규": 0, "갱신": 0, "보존(결과 확정)": 0, "보존(이미 시작)": 0, "보존(더 최신 회차)": 0}
    now_s = now_kst().strftime("%Y-%m-%d %H:%M")
    ts = now_kst().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]          # 밀리초까지 저장해 업로드 순서를 보존
    saved_path = saved_path or ""
    if raw_bytes is not None and not saved_path:
        stamp = now_kst().strftime("%Y%m%d_%H%M%S")
        saved_path = os.path.join(UPLOAD_DIR, f"{stamp}_{os.path.basename(source_file)}")
        with open(saved_path, "wb") as f:
            f.write(raw_bytes)
    if raw_bytes is None and saved_path and os.path.exists(saved_path):
        raw_bytes = open(saved_path, "rb").read()
    fhash = hashlib.sha1(raw_bytes).hexdigest() if raw_bytes else ""
    rows = df.to_dict("records")
    keys = ["|".join(str(x) for x in [r["match_datetime"], r["league"], r["bet_type"], r["line"], r["home_team"], r["away_team"]]) for r in rows]
    with _conn() as c:
        # 삭제·덮어쓰기 전에 기존 값을 읽어 둔다(배당 변경 이력, 결과 스코어 보존용)
        old = {}
        for i in range(0, len(keys), 400):
            ch = keys[i:i + 400]
            q = ("SELECT row_key, odds1, oddsx, odds2, uploaded_at, home_score, away_score, total_score, source_file, round_label FROM matches WHERE row_key IN (%s)"
                 % ",".join("?" * len(ch)))
            for rr in c.execute(q, ch):
                old[rr[0]] = rr
        c.execute("DELETE FROM matches WHERE source_file=?", (source_file,))
        # 더 최신 회차 파일이 이미 가진 경기는 이전 회차 파일이 줄을 추가하지도 못하게 막는다(정리된 옛 핸디 라인이 되살아나는 것 방지)
        locked = set()
        for g in {(r["match_datetime"], r["league"], r["home_team"], r["away_team"]) for r in rows}:
            for (rl_,) in c.execute("SELECT DISTINCT round_label FROM matches WHERE match_datetime=? AND league=? AND home_team=? AND away_team=? AND source_file<>?",
                                    (*g, source_file)):
                if not _round_ok(round_label, rl_):
                    locked.add(g)
                    break
        for r, key in zip(rows, keys):
            if (r["match_datetime"], r["league"], r["home_team"], r["away_team"]) in locked:
                stats["보존(더 최신 회차)"] += 1
                continue
            o = old.get(key)
            hs_, as_, tot_ = _ni(r["home_score"]), _ni(r["away_score"]), (_nz(r["total_score"]) if "total_score" in r else None)
            same_file = bool(o) and o[8] == source_file          # 같은 파일을 다시 읽는 경우(다시 읽기)는 보호 규칙 없이 갱신
            if o and not same_file:
                if o[5] is not None:                             # 결과가 확정된 경기 → 참고·학습용으로 보존
                    stats["보존(결과 확정)"] += 1
                    continue
                if not _round_ok(round_label, o[9]):             # 더 최신 회차가 이미 가진 경기 → 이전 회차 파일이 덮어쓰지 못함
                    stats["보존(더 최신 회차)"] += 1
                    continue
                if r["match_datetime"] < now_s and hs_ is None:  # 이미 시작한 경기 → 결과가 들어온 경우에만 갱신
                    stats["보존(이미 시작)"] += 1
                    continue
            stats["갱신" if o else "신규"] += 1
            if o:
                new_odds = (_nz(r["odds1"]), _nz(r["oddsx"]), _nz(r["odds2"]))
                old_odds = (o[1], o[2], o[3])
                has_both = old_odds[0] is not None and new_odds[0] is not None
                if has_both and any((a is None) != (b is None) or (a is not None and abs(a - b) > 0.004) for a, b in zip(old_odds, new_odds)):
                    c.execute("INSERT INTO odds_history(row_key, snap_at, odds1, oddsx, odds2, source_file) VALUES(?,?,?,?,?,?)", (key, o[4], o[1], o[2], o[3], o[8]))   # 바뀌기 '전' 배당 보관
                if hs_ is None and o[5] is not None:                       # 새 파일에 결과가 없어도 이미 있던 결과는 지우지 않는다
                    hs_, as_, tot_ = o[5], o[6], o[7]
            c.execute("""INSERT OR REPLACE INTO matches(row_key, source_file, round_label, game_no, match_datetime, league, sport,
                         bet_type, line, home_team, away_team, odds1, oddsx, odds2, home_score, away_score, uploaded_at, total_score,
                         trend1, trendx, trend2)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (key, source_file, round_label, str(r["game_no"]), r["match_datetime"], r["league"],
                       r["sport"], r["bet_type"], _nz(r["line"]), r["home_team"], r["away_team"],
                       _nz(r["odds1"]), _nz(r["oddsx"]), _nz(r["odds2"]),
                       hs_, as_, ts, tot_,
                       _ni(r["trend1"]) if "trend1" in r else 0,
                       _ni(r["trendx"]) if "trendx" in r else 0,
                       _ni(r["trend2"]) if "trend2" in r else 0))
        # '최신 것만': 새 파일에 있는 경기의, 새 파일에는 없는 예전 줄(바뀌기 전 핸디 라인 등)은 지운다(결과가 있는 줄은 보존)
        by_game = {}
        for r, key in zip(rows, keys):
            by_game.setdefault((r["match_datetime"], r["league"], r["home_team"], r["away_team"]), []).append(key)
        new_by_type = {}
        for r, key in zip(rows, keys):
            new_by_type.setdefault((r["match_datetime"], r["league"], r["home_team"], r["away_team"], r["bet_type"]), []).append((key, r["line"]))
        for (dt_, lg_, h_, a_), ks in by_game.items():
            if dt_ < now_s:                                               # 이미 시작한 경기의 줄은 건드리지 않는다
                continue
            stale = [x for x in c.execute("SELECT row_key, bet_type, line, odds1, oddsx, odds2, uploaded_at, source_file, round_label FROM matches WHERE match_datetime=? AND league=? "
                                          "AND home_team=? AND away_team=? AND home_score IS NULL AND row_key NOT IN (%s)" % ",".join("?" * len(ks)),
                                          (dt_, lg_, h_, a_, *ks)).fetchall() if _round_ok(round_label, x[8])]
            for sk, sbt, sline, so1, sox, so2, sts, ssrc, _rl in stale:   # 기준점(핸디·언더오버 라인)이 바뀐 경우를 이력으로 남긴다
                news = new_by_type.get((dt_, lg_, h_, a_, sbt), [])
                same_old = [x for x in stale if x[1] == sbt]
                if sbt in ("H", "U", "hH", "hU") and len(news) == 1 and len(same_old) == 1 and sline is not None and news[0][1] is not None and sline != news[0][1]:
                    c.execute("INSERT INTO odds_history(row_key, snap_at, odds1, oddsx, odds2, source_file, old_line) VALUES(?,?,?,?,?,?,?)",
                              (news[0][0], sts, so1, sox, so2, ssrc, sline))
            if stale:
                c.execute("DELETE FROM matches WHERE row_key IN (%s)" % ",".join("?" * len(stale)), [x[0] for x in stale])
        c.execute("INSERT OR REPLACE INTO uploads VALUES(?,?,?,?,?,?)",
                  (source_file, round_label, fhash, ts, saved_path, len(df)))
    if saved_path:
        storage.push(_rel(saved_path))
    if sync:
        _sync_db()
    return stats


def _nz(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def _ni(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else int(v)


# ── 백업/복구 (Streamlit Cloud 는 재배포·재시작 시 디스크가 초기화되므로 필수) ──
def export_backup():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(DATA_DIR):
            for fn in files:
                p = os.path.join(root, fn)
                z.write(p, os.path.relpath(p, DATA_DIR))
    return buf.getvalue()


def import_backup(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        z.extractall(DATA_DIR)


# ───────────────────── 입력(PDF/이미지) → 텍스트 ─────────────────────
def _strip_underlines(gray):
    """링크 배당(1.09)에 밑줄이 있어 '309' 처럼 잘못 읽히는 문제를 줄이기 위해 가로로 긴 직선을 지운다."""
    import numpy as np
    from PIL import Image
    a = np.array(gray)
    b = a < 150
    k = max(14, int(a.shape[1] / 110))                  # 밑줄은 글자 가로획보다 훨씬 길다
    for y in range(b.shape[0]):
        row = b[y]
        if row.sum() < k:
            continue
        x, n = 0, len(row)
        while x < n:
            if row[x]:
                st = x
                while x < n and row[x]:
                    x += 1
                if x - st >= k:
                    a[y, st:x] = 255
            else:
                x += 1
    return Image.fromarray(a)


def _ocr_pages(images, progress=None):
    import pytesseract
    from PIL import ImageOps
    texts = []
    for i, im in enumerate(images):
        im = im.convert("L")
        if im.width < 2400:                       # 작은 글씨 → 확대
            f = 2400 / im.width
            im = im.resize((int(im.width * f), int(im.height * f)))
        im = _strip_underlines(ImageOps.autocontrast(im))
        texts.append(pytesseract.image_to_string(im, lang="kor+eng", config="--psm 6"))
        if progress:
            progress((i + 1) / len(images))
    return "\n".join(texts)


def reparse_saved(source_file=None):
    """저장돼 있는 원본(PDF/이미지/텍스트)을 현재 파서로 다시 읽어 DB 를 갱신. ※ 검수 표에서 손으로 고친 값은 원본 기준으로 되돌아간다."""
    with _conn() as c:
        q, a = "SELECT source_file, round_label, saved_path FROM uploads", ()
        if source_file:
            q, a = q + " WHERE source_file=?", (source_file,)
        rows = c.execute(q, a).fetchall()
    ok, fail = 0, []
    for sf, lab, path in rows:
        if path and not os.path.exists(path):
            storage.fetch(_rel(path))                     # 로컬에 없으면 R2 에서 내려받는다(시작 시에는 큰 파일을 받지 않으므로)
        if not path or not os.path.exists(path):
            fail.append(f"{sf} (원본 없음)")
            continue
        try:
            raw = open(path, "rb").read()
            text, _ = file_to_text(path, raw)
            df = parse_text(text, year=year_from_name(sf))
            if df.empty:
                fail.append(f"{sf} (읽힌 경기 없음)")
                continue
            save_matches(df, sf, lab, saved_path=path)
            ok += 1
        except Exception as e:
            fail.append(f"{sf} ({e})")
    return ok, fail


def rebuild_from_storage(progress=None):
    """DB 가 비었을 때: R2 의 uploads/ 에 보관된 원본(PDF·이미지·텍스트)을 다시 읽어 DB 를 복구한다.
    같은 파일명이 여러 번 올라가 있으면 가장 최근 것만 쓰고, 이미 DB 에 있는 파일은 건너뛴다."""
    init_sports_db()
    found = {}
    for rel, _ in storage.list_keys():
        m = re.fullmatch(r"uploads/(\d{8}_\d{6})_(.+)", rel)          # 하위 폴더(uploads/lotto/…)는 제외 — 토토 원본만
        if m and "/" not in m.group(2) and m.group(2).lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".txt")):
            found[m.group(2)] = (m.group(1), rel)                      # 같은 이름은 최신 스탬프가 이김
    with _conn() as c:
        have = {r[0] for r in c.execute("SELECT source_file FROM uploads")}
    todo = sorted(((st, name, rel) for name, (st, rel) in found.items() if name not in have), key=lambda x: x[0])
    parsed, fail = {}, []
    for i, (stamp, name, rel) in enumerate(todo):
        try:
            path = storage.fetch(rel)
            if not path:
                fail.append(f"{name} (다운로드 실패)")
                continue
            raw = open(path, "rb").read()
            text, _ = file_to_text(path, raw)
            df = parse_text(text, year=year_from_name(name))
            if df.empty:
                fail.append(f"{name} (읽힌 경기 없음)")
                continue
            parsed[name] = (df, make_round_label(name, text) or "미지정", path)
        except Exception as e:
            fail.append(f"{name} ({e})")
        if progress:
            progress((i + 1) / len(todo))
    if parsed:
        final = resolve_round_labels({n: v[1] for n, v in parsed.items()})
        for n, (df, _, path) in parsed.items():
            save_matches(df, n, final[n], saved_path=path, sync=False)
        _sync_db()
    return len(parsed), len(found) - len(todo), fail


def file_to_text(name, raw, progress=None):
    """텍스트 레이어가 있으면 그대로(정확), 없으면 OCR(부정확할 수 있음). return (text, method)"""
    from PIL import Image
    if name.lower().endswith(".txt"):
        return raw.decode("utf-8", "ignore"), "텍스트"
    if name.lower().endswith(".pdf"):
        try:                                           # 1순위: pdfium(C 라이브러리, pypdf 보다 약 7배 빠름)
            import pypdfium2 as _pdfium
            _doc = _pdfium.PdfDocument(raw)
            _txt = "\n".join(_doc[i].get_textpage().get_text_range() for i in range(len(_doc)))
            if len(re.findall(r"\d{1,2}\.\d{2}", _txt)) >= 10:
                return _txt, "PDF 텍스트"
        except Exception:
            pass
        try:
            from pypdf import PdfReader
            txt = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
            if len(re.findall(r"\d{1,2}\.\d{2}", txt)) >= 10:
                return txt, "PDF 텍스트"
        except Exception:
            pass
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(raw)
        imgs = [pdf[i].render(scale=4).to_pil() for i in range(len(pdf))]
        return _ocr_pages(imgs, progress), "OCR(이미지 PDF)"
    return _ocr_pages([Image.open(io.BytesIO(raw))], progress), "OCR(이미지)"


# ───────────────────── 텍스트 → 행 파싱 ─────────────────────
CIRCLED = {"①":1,"②":2,"③":3,"④":4,"⑤":5,"⑥":6,"⑦":7,"⑧":8,"⑨":9,"⑩":10}
RESULT_WORDS = {"홈승", "홈패", "핸디승", "핸디패", "핸디무", "언더", "오버", "홀", "짝", "무"} | set(CIRCLED)
STATUS_WORDS = {"경기전", "결기전", "경기건", "종료", "하프타임"} | RESULT_WORDS
LEAGUE_STRIP = r"남축INTL|C네이션|남농EASL|KOVO[남여]|WKBL|KBL|NBA|MLS|A리그|J1리그|EASL|SUM|승.패"
LEAGUES = [("WKBL", r"WKBL|여농"), ("KBL", r"KBL"), ("NBA", r"NBA|N8A|NEA|1484"), ("KOVO남", r"KOVO\s*남"),
           ("KOVO여", r"KOVO\s*여"), ("MLS", r"MLS"), ("A리그", r"A리그"), ("J1리그", r"J1?리그"),
           ("EASL", r"EASL"), ("KBO", r"KBO"), ("MLB", r"MLB"), ("NPB", r"NPB")]
SOCCER_HINT = r"ACL|UCL|UEL|UECL|EFL|챔|세리에|FA컵|INTL|네이션|월드컵|친선|유로|예선|챔피언스|유로파|컨퍼런스|리그|컵|EPL|라리가|분데스|세리에|리그1|MLS|남축|여축"
VOLLEY_HINT = r"KOVO|V리그|배구"
BASKET_HINT = r"NBA|KBL|WKBL|EASL|CBA|농구|남농|여농"
BASEBALL_HINT = r"KBO|MLB|NPB|야구"


def sport_of_league(lg):
    """알려진 리그면 그대로, 아니면 이름의 키워드로 종목 추정(못 맞히면 None → 배당 라인 크기로 추정)"""
    if lg in SPORT_OF:
        return SPORT_OF[lg]
    for pat, sp in ((BASEBALL_HINT, "야구"), (VOLLEY_HINT, "배구"), (BASKET_HINT, "농구"), (SOCCER_HINT, "축구")):
        if re.search(pat, lg or "", re.I):
            return sp
    return None
SPORT_OF = {"NBA": "농구", "KBL": "농구", "WKBL": "농구", "EASL": "농구", "KOVO남": "배구", "KOVO여": "배구",
            "KBO": "야구", "MLB": "야구", "NPB": "야구", "MLS": "축구", "A리그": "축구", "J1리그": "축구", "국가대표": "축구"}
DEFAULT_TOTAL = {"축구": 2.6, "농구": 215.0, "배구": 195.0, "야구": 8.5}
SIGMA = {"NBA": 13.0, "KBL": 11.5, "WKBL": 10.5, "EASL": 12.0}


def _fix_odds_token(tok):
    tok = tok.replace(",", ".").strip("!|[]()↑↓")
    if re.fullmatch(r"\d{1,2}\.\d{2}", tok):
        return float(tok)
    if re.fullmatch(r"\d{3,4}", tok):                 # OCR 이 소수점을 놓친 경우 180 → 1.80, 2100 → 21.00
        v = float(tok[:-2] + "." + tok[-2:])
        return v
    return None


def _parse_line_value(s, kind):
    s = s.replace(",", ".")
    m = re.search(r"[+-]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    t = m.group(0)
    if "." not in t:
        sign = -1 if t.startswith("-") else 1
        d = t.lstrip("+-")
        if kind == "H":
            v = float(d) if len(d) == 1 else float(d[:-1] + "." + d[-1])
        else:
            v = float(d) if len(d) <= 1 else (float(d[:-1] + "." + d[-1]) if len(d) <= 2 or len(d) == 4 else float(d))
        return sign * v if kind == "H" else v
    return float(t)


def parse_text(text, default_date=None, year=None):
    text = re.sub(r"[\x00-\x08\x0b-\x1f\u00a0]", " ", text)      # PDF 가 칸 사이에 넣는 제어문자 제거
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    rows, prev_dt = [], None
    cur_year = year or detect_year(text) or (default_date or now_kst()).year     # 연도: 파일명/머리말 우선, 없으면 올해
    for ln in lines:
        toks = ln.split()
        di = next((i for i, t in enumerate(toks) if re.match(r"^\d{1,2}\.\d{2}", t)), None)
        if di is None:
            continue
        m = re.match(r"^(\d{1,2})\.(\d{2})", toks[di])
        mo, dd = int(m.group(1)), int(m.group(2))
        ti = next((i for i in range(di + 1, min(di + 4, len(toks)))
                   if re.fullmatch(r"\(?\d{1,2}:?\d{2}\)?", toks[i])), None)
        if ti is None:
            continue
        tm = re.sub(r"\D", "", toks[ti]).zfill(4)
        hh, mi = int(tm[:-2]), int(tm[-2:])
        try:
            dt = datetime(cur_year, mo, dd, hh, mi)
        except ValueError:
            dt = prev_dt                                 # OCR 오인식 → 직전 행 시각
        if dt is not None and prev_dt is not None and (prev_dt - dt).days > 200:
            cur_year += 1                                # 12월 → 1월로 넘어가는 회차
            try:
                dt = datetime(cur_year, mo, dd, hh, mi)
            except ValueError:
                dt = prev_dt
        if dt is None:
            continue
        if prev_dt and dt < prev_dt and (prev_dt - dt) < timedelta(days=2):
            dt = prev_dt                                 # 목록은 시간순 → 역행은 OCR 오류
        prev_dt = dt

        id_tok = toks[di - 1] if di > 0 and re.fullmatch(r"\d{3,4}", toks[di - 1]) else ""
        # 표의 열 순서: 번호 일시 리그 유형 홈팀 vs 원정팀 → 시간 바로 뒤 첫 칸이 '리그'(목록에 없는 리그도 인식)
        lg_tok = toks[ti + 1] if ti + 1 < len(toks) else ""
        if re.fullmatch(r"(?i)(H|U|SUM|hH|hU|h)", lg_tok) or re.fullmatch(r"[+-]?\d+(\.\d+)?", lg_tok):
            lg_tok, rest = "", " ".join(toks[ti + 1:])
        else:
            rest = " ".join(toks[ti + 2:])

        # 유형 (전반 경기: h(전반) / h H -1.0 / h U 1.5 는 90분 경기와 별개의 유형)
        bet, line, half = "일반", None, False
        for pat, hb in ((r"\s*h\s*\(\s*전반\s*\)\s*", "h"), (r"\s*h\s*H\s*([+-]?\d+(?:[.,]\d)?)\s*", "hH"), (r"\s*h\s*U\s*(\d+(?:[.,]\d)?)\s*", "hU")):
            mhf = re.match(pat, rest)
            if mhf:
                bet, half = hb, True
                if hb == "hH": line = _parse_line_value(mhf.group(1).replace(" ", ""), "H")
                if hb == "hU": line = _parse_line_value(mhf.group(1), "U")
                rest = rest[mhf.end():]
                break
        mh = re.search(r"\bH\s*([+-]?\s*\d+(?:[.,]\d)?)", rest)
        mu = re.search(r"\b[UuO]\s*(\d+(?:[.,]\d)?)", rest)
        mw = re.search(r"승(\S)패", rest) if not half else None
        if half:
            pass
        elif mw:
            ch = mw.group(1)
            n_ = CIRCLED[ch] if ch in CIRCLED else (int(ch) if ch in "0123456789" else 5)
            bet, line = f"승{n_}패", float(n_)
        elif re.search(r"\bs[uv]m\b", rest, re.I):
            bet = "SUM"
        elif mh:
            bet, line = "H", _parse_line_value(mh.group(1).replace(" ", ""), "H")
        elif mu:
            bet, line = "U", _parse_line_value(mu.group(1), "U")

        league = next((k for k, p in LEAGUES if re.search(p, lg_tok, re.I)), None) or (lg_tok or None)

        # 점수 (일반 행에서만 의미 있음)
        hs = as_ = None
        ms = re.search(r"(\d{1,3})\s*:\s*(\d{1,3})\b", rest)
        finished = bool(toks) and toks[-1] in RESULT_WORDS
        if ms and bet == "일반" and finished:
            hs, as_ = int(ms.group(1)), int(ms.group(2))

        # 팀/배당 영역 분리: 마지막 한글 단어(상태칩 제외) 뒤를 배당으로
        clean = re.sub(LEAGUE_STRIP, lambda m_: " " * len(m_.group(0)), rest)
        kor = [(m2.start(), m2.end(), m2.group(0)) for m2 in re.finditer(r"[가-힣A-Za-z][가-힣A-Za-z0-9*]+", clean)]
        team_words = [w for w in kor if w[2] not in STATUS_WORDS]
        odds_start = team_words[-1][1] if team_words else 0
        odds_txt = rest[odds_start:]
        odds, trends = [], []
        for t in re.split(r"\s+", odds_txt):
            v = _fix_odds_token(t)
            if v is not None and 1.01 <= v <= 60:
                odds.append(v)
                trends.append(1 if "↑" in t else (-1 if "↓" in t else 0))      # ↑ 배당 상승 / ↓ 배당 하락(사이트 표시)
        total_score = None
        if bet in ("U", "SUM") and finished and len(team_words) >= 2:
            mid = clean[team_words[0][1]:team_words[-1][0]]        # 홈팀과 원정팀 사이의 숫자 = 총 득점(배구는 세트가 아니라 총 점수)
            mt = re.search(r"\d+(?:\.\d+)?", mid)
            total_score = float(mt.group(0)) if mt else None
        names = [w[2] for w in team_words]
        home = names[0] if names else ""
        away = names[-1] if len(names) > 1 else ""

        rows.append(dict(game_no=id_tok, match_datetime=dt.strftime("%Y-%m-%d %H:%M"), league=league,
                         bet_type=bet, line=line, home_team=home, away_team=away, odds_raw=odds[:3], trend_raw=trends[:3],
                         home_score=hs, away_score=as_, total_score=total_score, _names=names))
    return _finalize(rows)


def _finalize(rows):
    if not rows:
        return pd.DataFrame()
    # 게임 그룹: '일반' 행이 새 게임의 시작
    gid, cur = [], -1
    for r in rows:
        if r["bet_type"] == "일반":
            cur += 1
        gid.append(max(cur, 0))
    for r, g in zip(rows, gid):
        r["_g"] = g
    out = []
    by_g = {}
    for r in rows:
        by_g.setdefault(r["_g"], []).append(r)
    last_league = None
    for g, grp in by_g.items():
        leagues = [r["league"] for r in grp if r["league"]]
        league = Counter(leagues).most_common(1)[0][0] if leagues else last_league
        last_league = league or last_league
        names = Counter()
        for r in grp:
            if r["home_team"]:
                names[("h", r["home_team"])] += 1
            if r["away_team"]:
                names[("a", r["away_team"])] += 1
        home = next((n for (k, n), _ in names.most_common() if k == "h"), "")
        away = next((n for (k, n), _ in names.most_common() if k == "a"), "")
        big_line = any((r["line"] or 0) >= 100 for r in grp if r["bet_type"] == "U")
        sport = sport_of_league(league) or ("농구" if big_line else "축구")
        soccer3 = sport == "축구"
        for r in grp:
            o, tr = r["odds_raw"], r["trend_raw"]
            o1 = ox = o2 = None
            t1 = tx = t2 = 0
            three = (soccer3 and r["bet_type"] in ("일반", "H", "h", "hH")) or is_margin_bet(r["bet_type"])
            if three and len(o) >= 3:
                o1, ox, o2 = o[0], o[1], o[2]
                t1, tx, t2 = tr[0], tr[1], tr[2]
            elif len(o) >= 2:
                o1, o2 = o[0], o[1]
                t1, t2 = tr[0], tr[1]
            out.append(dict(game_no=r["game_no"], match_datetime=r["match_datetime"], league=league or "기타",
                            sport=sport, bet_type=r["bet_type"], line=r["line"], home_team=home, away_team=away,
                            odds1=o1, oddsx=ox, odds2=o2, home_score=r["home_score"], away_score=r["away_score"],
                            total_score=r.get("total_score"), trend1=t1, trendx=tx, trend2=t2))
    df = pd.DataFrame(out)
    return df.drop_duplicates(subset=["match_datetime", "league", "bet_type", "line", "home_team", "away_team"],
                              keep="last").reset_index(drop=True)


def _num_from_name(name):
    m = re.search(r"(\d{1,4})\s*회\s*차?", os.path.basename(name))
    return int(m.group(1)) if m else None


def year_from_name(name):
    """파일명에서 연도: '2026년도', '2026년' → 2026"""
    m = re.search(r"(20\d{2})\s*년", os.path.basename(name))
    return int(m.group(1)) if m else None


def header_round(text):
    """본문 머리말(예: '2026년도 137회차')에서 (연도, 회차). 사이드바의 '승부식 136회차 마감' 등은 쓰지 않는다."""
    m = re.search(r"(20\d{2})\s*년도?\s*(\d{2,3})\s*회\s*차", text)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def detect_year(text):
    y, _ = header_round(text)
    if y:
        return y
    m = re.search(r"(20\d{2})\s*년도", text)
    return int(m.group(1)) if m else None


def make_round_label(name, text=""):
    """파일명이 무엇이든: 회차는 파일명 → 본문 머리말 순, 연도는 파일명 → 본문 머리말 순으로 찾아 '2026년 137회차' 로 만든다.
    연도를 못 찾으면 '137회차'."""
    hy, hn = header_round(text)
    n = _num_from_name(name) or hn
    if n is None:
        m = re.search(r"(\d{2,3})\s*회\s*차\s*대상경기", text)
        n = int(m.group(1)) if m else None
    y = year_from_name(name) or hy or detect_year(text)
    if n is None:
        return ""
    return f"{y}년 {n}회차" if y else f"{n}회차"


def round_from_name(name):
    n = _num_from_name(name)
    if n is None:
        return ""
    y = year_from_name(name)
    return f"{y}년 {n}회차" if y else f"{n}회차"


def guess_round_label(text):
    return make_round_label("", text)


def round_sort_key(label):
    """'2026년 137회차-2' → (2026, 137, 2) 로 정렬(문자열 정렬이면 99회차가 137회차보다 크게 나오는 문제 방지)"""
    m = re.match(r"(?:(\d{4})년\s*)?(\d+)회차(?:-(\d+))?", str(label))
    return (int(m.group(1) or 0), int(m.group(2)), int(m.group(3) or 0)) if m else (0, -1, 0)


def resolve_round_labels(batch):
    """batch: {source_file: 회차라벨}. 같은 회차 파일이 여러 개면 '132회차-1', '132회차-2' … 로 구분(업로드 순).
    이미 저장된 같은 회차 파일도 함께 번호를 다시 매긴다. return {source_file: 최종라벨}"""
    base_of = lambda lab: re.sub(r"-\d+$", "", lab or "미지정")
    now_dt = now_kst()
    with _conn() as c:
        existing = {sf: (lab, ts) for sf, lab, ts in c.execute("SELECT source_file, round_label, uploaded_at FROM uploads")}
    members = {}
    for sf, (lab, ts) in existing.items():
        if sf not in batch:
            members.setdefault(base_of(lab), []).append((ts, sf))
    for i, (sf, lab) in enumerate(batch.items()):
        b = base_of(lab)
        ts = existing[sf][1] if sf in existing and base_of(existing[sf][0]) == b else (now_dt + timedelta(milliseconds=i)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]   # 올린 순서 유지
        members.setdefault(b, []).append((ts, sf))
    final, changed = {}, False
    with _conn() as c:
        for b, lst in members.items():
            lst.sort()
            for i, (_, sf) in enumerate(lst, 1):
                label = b if len(lst) == 1 else f"{b}-{i}"
                if sf in batch:
                    final[sf] = label
                elif existing[sf][0] != label:
                    c.execute("UPDATE uploads SET round_label=? WHERE source_file=?", (label, sf))
                    c.execute("UPDATE matches SET round_label=? WHERE source_file=?", (label, sf))
                    changed = True
    if changed:
        _sync_db()
    return final


# ───────────────────── 확률 모델 ─────────────────────
def devig(odds):
    """배당 → 진확률 (power 방식: Σ(1/o)^c = 1 이 되는 c 를 이분법으로)"""
    inv = [1 / o for o in odds]
    if abs(sum(inv) - 1) < 1e-9:
        return inv
    lo, hi = 0.5, 4.0
    for _ in range(60):
        c = (lo + hi) / 2
        s = sum(p ** c for p in inv)
        lo, hi = (c, hi) if s > 1 else (lo, c)
    ps = [p ** ((lo + hi) / 2) for p in inv]
    t = sum(ps)
    return [p / t for p in ps]


_FACT = None


def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _score_matrix(lh, la, n=14):
    """홈·원정 득점 푸아송 확률표 (numpy, n×n)"""
    import numpy as np
    global _FACT
    if _FACT is None or len(_FACT) < 40:
        _FACT = np.array([math.factorial(k) for k in range(40)], dtype=float)
    k = np.arange(n)
    ph = np.exp(-lh) * lh ** k / _FACT[:n]
    pa = np.exp(-la) * la ** k / _FACT[:n]
    return np.outer(ph, pa)


def _outcome_probs(M):
    import numpy as np
    return float(np.tril(M, -1).sum()), float(np.trace(M)), float(np.triu(M, 1).sum())


def fit_poisson(p_home, p_draw, p_away, total):
    """승/무/패 진확률과 총득점 기대치로 (홈 기대득점, 원정 기대득점) 역산"""
    best, best_err = (total / 2, total / 2), 9
    for k in range(-60, 61):
        d = k * 0.05
        lh, la = max(0.05, (total + d) / 2), max(0.05, (total - d) / 2)
        ph, px, pa = _outcome_probs(_score_matrix(lh, la))
        if p_draw is None:                              # 무승부 없는 종목: 무 제외 후 비교
            s = ph + pa
            err = (ph / s - p_home / (p_home + p_away)) ** 2
        else:
            err = (ph - p_home) ** 2 + (px - p_draw) ** 2 + (pa - p_away) ** 2
        if err < best_err:
            best, best_err = (lh, la), err
    return best


def _set_win_prob(q):                                   # 5세트제 매치 승리확률
    return q**3 * (1 + 3 * (1 - q) + 6 * (1 - q) ** 2)


def fit_set_prob(p_match):
    lo, hi = 0.01, 0.99
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _set_win_prob(mid) < p_match else (lo, mid)
    return (lo + hi) / 2


def _resid_records(r):
    """종료된 '일반' 경기 1행 → [(키, 실제결과-배당내재확률)] (팀별 + 종목 전체 홈 편향)"""
    if r["bet_type"] != "일반" or pd.isna(r["home_score"]) or not has_odds(r):
        return []
    odds = [r["odds1"]] + ([r["oddsx"]] if pd.notna(r["oddsx"]) else []) + [r["odds2"]]
    try:
        p = devig(odds)
    except Exception:
        return []
    hw = 1.0 if r["home_score"] > r["away_score"] else 0.0
    aw = 1.0 if r["away_score"] > r["home_score"] else 0.0
    return [(r["home_team"], hw - p[0]), (r["away_team"], aw - p[-1]), (f"__sport:{r['sport']}", hw - p[0])]


def team_form(learn_df):
    form = {}
    if learn_df.empty:
        return form
    for r in learn_df.to_dict("records"):
        for k, v in _resid_records(r):
            form.setdefault(k, []).append(v)
    return {t: (sum(v) / len(v), len(v)) for t, v in form.items()}


def _adj(form, team, k=12, cap=0.05):
    if team not in form:
        return 0.0, 0
    mean, n = form[team]
    shrink = n / (n + k)                                # 표본이 적을수록 거의 0
    return max(-cap, min(cap, 0.5 * shrink * mean)), n


def confidence_label(p, n_way=2):
    thr = (0.65, 0.52, 0.42) if n_way == 2 else (0.58, 0.46, 0.38)
    if p >= thr[0]:
        return "🟢 강력"
    if p >= thr[1]:
        return "🟡 유력"
    if p >= thr[2]:
        return "🟠 낮음"
    return "🔴 위험"


HALF_TYPES = ("h", "hH", "hU")
TYPE_GROUP = {"일반": "일반", "H": "핸디", "U": "언더오버", "SUM": "홀짝", "h": "전반일반", "hH": "전반핸디", "hU": "전반언더오버"}


def type_group(bt):
    return TYPE_GROUP.get(bt, "승N패")


def is_margin_bet(bt):
    return bool(re.fullmatch(r"승\d+패", str(bt)))


def has_odds(r):
    return pd.notna(r["odds1"]) and pd.notna(r["odds2"])


def odds_ok(r):
    """배당이 정상적으로 읽혔는지 검사: 역수합(1/o 합)이 1.02~1.45 안이어야 정상(발매사 마진 5~30%)"""
    o = [r["odds1"], r["oddsx"], r["odds2"]]
    o = [x for x in o if x is not None and not pd.isna(x)]
    if len(o) < 2:
        return False
    return 1.02 <= sum(1 / x for x in o) <= 1.45


def _handicap_crosscheck(rows, sport, ph, px, pa, three):
    """축구 핸디 라인(정수)의 '핸디무' 확률로 일반 승/무/패를 교차검증.
    H -1.0 : 핸디무 = 홈이 정확히 1점차로 이김 → 홈승 = 핸디승+핸디무,  핸디패 = 무+원정승  ⇒ 무 = 핸디패 − 원정승
    H +1.0 : 핸디무 = 원정이 정확히 1점차로 이김 → 원정승 = 핸디무+핸디패, 핸디승 = 홈승+무  ⇒ 무 = 핸디승 − 홈승
    H ±2 이상 : 정확한 등식은 못 세우지만 '홈(원정)승 ≥ 핸디승+핸디무' 라는 하한이 생겨, 일반배당이 그보다 낮으면 끌어올림.
    핸디무가 높다 = 한쪽이 딱 그 점수차로 이길 가능성이 크다 = 무승부가 아니라 승패가 갈릴 가능성이 크다."""
    if sport != "축구" or not three:
        return ph, px, pa, None
    ests, notes = [], []
    for r in rows:
        if not (r["bet_type"] == "H" and pd.notna(r["line"]) and pd.notna(r["oddsx"]) and has_odds(r) and odds_ok(r)):
            continue
        line = r["line"]
        if abs(line - round(line)) > 1e-9 or abs(line) < 1:
            continue
        k = int(abs(line))
        w, d, l = devig([r["odds1"], r["oddsx"], r["odds2"]])
        if line < 0 and k == 1:
            px_h = min(max(l - pa, 0.03), l - 0.01)
            ests.append((w + d, px_h, l - px_h, 0.3))
            notes.append(f"핸디-1 핸디패 {l*100:.0f}% = 무 {px_h*100:.0f}% + 원정승 {(l-px_h)*100:.0f}% · 핸디무 {d*100:.0f}% = 홈 1점차 승 → 핸디시장 홈승 {(w+d)*100:.0f}%·무 {px_h*100:.0f}% (일반배당 홈승 {ph*100:.0f}%·무 {px*100:.0f}%)")
        elif line > 0 and k == 1:
            px_h = min(max(w - ph, 0.03), w - 0.01)
            ests.append((w - px_h, px_h, d + l, 0.3))
            notes.append(f"핸디+1 핸디승 {w*100:.0f}% = 홈승 {(w-px_h)*100:.0f}% + 무 {px_h*100:.0f}% · 핸디무 {d*100:.0f}% = 원정 1점차 승 → 핸디시장 원정승 {(d+l)*100:.0f}%·무 {px_h*100:.0f}% (일반배당 원정승 {pa*100:.0f}%·무 {px*100:.0f}%)")
        elif line < 0 and w + d > ph:
            f = (1 - (w + d)) / max(1e-9, px + pa)
            ests.append((w + d, px * f, pa * f, 0.2))
            notes.append(f"핸디{line:+g} 핸디승+무 {(w+d)*100:.0f}% ≥ 일반 홈승 → 홈승 하한으로 상향")
        elif line > 0 and d + l > pa:
            f = (1 - (d + l)) / max(1e-9, ph + px)
            ests.append((ph * f, px * f, d + l, 0.2))
            notes.append(f"핸디{line:+g} 핸디무+패 {(d+l)*100:.0f}% ≥ 일반 원정승 → 원정승 하한으로 상향")
    if not ests:
        return ph, px, pa, None
    wsum = min(0.6, sum(e[3] for e in ests))
    sc = wsum / sum(e[3] for e in ests)
    nb = [(1 - wsum) * v for v in (ph, px, pa)]
    for e in ests:
        for i in range(3):
            nb[i] += e[3] * sc * e[i]
    t = sum(nb)
    return nb[0] / t, nb[1] / t, nb[2] / t, " | ".join(notes) + f" · 일반 {int((1-wsum)*100)}:핸디 {int(wsum*100)} 반영"


def _basketball_h_check(rows, league, ph, pa):
    """농구 핸디(±.5 라인) 커버확률 → 점수차 분포(정규)를 역산해 '핸디시장이 보는 일반 승 확률'을 구하고 일반 70 : 핸디 30 으로 합성.
    (점수차 ~ N(mu, sigma),  P(홈이 라인을 커버) = Φ((mu+line)/sigma)  ⇒  mu = sigma·z(커버확률) − line)"""
    sig = SIGMA.get(league, 12.0)
    nd = NormalDist()
    ests, notes = [], []
    for r in rows:
        if r["bet_type"] != "H" or pd.isna(r["line"]) or not has_odds(r) or pd.notna(r["oddsx"]) or not odds_ok(r):
            continue
        cov = devig([r["odds1"], r["odds2"]])[0]
        mu = sig * nd.inv_cdf(min(max(cov, 0.03), 0.97)) - r["line"]
        ph_h = nd.cdf(mu / sig)
        ests.append(ph_h)
        notes.append(f"핸디{r['line']:+g} 홈커버 {cov*100:.0f}% → 핸디시장이 보는 홈승 {ph_h*100:.0f}% (일반배당 {ph/(ph+pa)*100:.0f}%)")
    if not ests:
        return ph, pa, None
    ph_h = sum(ests) / len(ests)
    tot = ph + pa
    new_ph = 0.7 * ph / tot + 0.3 * ph_h
    return new_ph * tot, (1 - new_ph) * tot, " | ".join(notes[:2]) + " · 일반 70:핸디 30 반영"


def _volleyball_h_check(rows, ph, pa):
    """배구 세트 핸디(±1.5) 커버확률에서 '세트 승률 q'를 역산 → 핸디시장이 보는 일반 승 확률을 구해 일반 70 : 핸디 30 으로 합성"""
    ests, notes = [], []
    for r in rows:
        if r["bet_type"] != "H" or pd.isna(r["line"]) or not has_odds(r) or pd.notna(r["oddsx"]) or not odds_ok(r):
            continue
        cov = devig([r["odds1"], r["odds2"]])[0]
        lo, hi = 0.02, 0.98
        for _ in range(50):
            q = (lo + hi) / 2
            c = sum(v for (h, a), v in _vb_scores(q).items() if (h - a) + r["line"] > 1e-9)
            lo, hi = (q, hi) if c < cov else (lo, q)
        ph_h = _set_win_prob((lo + hi) / 2)
        ests.append(ph_h)
        notes.append(f"핸디{r['line']:+g} 홈커버 {cov*100:.0f}% → 핸디시장이 보는 홈승 {ph_h*100:.0f}% (일반배당 {ph/(ph+pa)*100:.0f}%)")
    if not ests:
        return ph, pa, None
    ph_h, tot = sum(ests) / len(ests), ph + pa
    new_ph = 0.7 * ph / tot + 0.3 * ph_h
    return new_ph * tot, (1 - new_ph) * tot, " | ".join(notes[:2]) + " · 일반 70:핸디 30 반영"


def draw_level(px, ph, pa):
    """축구 무승부 가능성 등급 (전체 평균 무승부 비율 약 26%)"""
    if px >= 0.28 or (px >= 0.25 and abs(ph - pa) < 0.10):
        return f"🟣 무 가능성 높음 ({px*100:.0f}%)"
    if px >= 0.22:
        return f"⚪ 무 가능성 보통 ({px*100:.0f}%)"
    return f"▫️ 무 가능성 낮음 ({px*100:.0f}%)"


def upset_level(up, ph, pa):
    if abs(ph - pa) < 0.10:
        return f"⚖️ 박빙 (역배 {up*100:.0f}%)"
    if up >= 0.35:
        return f"🔥 이변 가능성 높음 ({up*100:.0f}%)"
    if up >= 0.25:
        return f"⚠️ 이변 주의 ({up*100:.0f}%)"
    return f"🛡️ 이변 낮음 ({up*100:.0f}%)"


def _trend_note(base, ph, pa):
    """① 내가 올린 파일들에서 읽은 실제 배당 변화(처음→현재) ② 없으면 사이트가 표시한 ↑↓ 를 문장으로"""
    if base is None:
        return ""
    fav_home = ph >= pa
    notes = []
    for side, cur, op, is_fav in (("홈", base.get("odds1"), base.get("open1"), fav_home), ("원정", base.get("odds2"), base.get("open2"), not fav_home)):
        if cur is not None and op is not None and pd.notna(cur) and pd.notna(op) and op > 0 and pd.isna(base.get("open_line")):
            pct = (cur - op) / op * 100
            if abs(pct) >= 2:
                notes.append(f"{'인기' if is_fav else '역배'} 쪽({side}) {op:g}→{cur:g} ({pct:+.0f}%)")
    if notes:
        return "배당변동: " + ", ".join(notes)
    t1, t2 = base.get("trend1") or 0, base.get("trend2") or 0
    t1 = 0 if pd.isna(t1) else int(t1); t2 = 0 if pd.isna(t2) else int(t2)
    t_fav, t_dog = (t1, t2) if fav_home else (t2, t1)
    out = []
    if t_dog < 0: out.append("역배 쪽 배당 하락↓")
    if t_dog > 0: out.append("역배 쪽 배당 상승↑")
    if t_fav > 0: out.append("인기 쪽 배당 상승↑")
    if t_fav < 0: out.append("인기 쪽 배당 하락↓")
    return ("사이트 표시 " + ", ".join(out)) if out else ""


def predict_game(rows, form):
    """rows: 같은 경기의 행들(일반/H/U/SUM). return {row_index: dict}"""
    base = next((r for r in rows if r["bet_type"] == "일반"), None)
    res = {}
    sport = rows[0]["sport"]; league = rows[0]["league"]
    home, away = rows[0]["home_team"], rows[0]["away_team"]
    uline = next((r["line"] for r in rows if r["bet_type"] == "U" and r["line"]), None)
    total = uline if uline else DEFAULT_TOTAL.get(sport, 2.6)

    model = None
    if base and has_odds(base) and odds_ok(base):
        three = base["oddsx"] is not None and not pd.isna(base["oddsx"])
        p = devig([base["odds1"]] + ([base["oddsx"]] if three else []) + [base["odds2"]])
        ph, px, pa = (p[0], p[1], p[2]) if three else (p[0], 0.0, p[1])
        ph_m, pa_m = ph, pa
        ph, px, pa, hnote = _handicap_crosscheck(rows, sport, ph, px, pa, three)
        if sport == "농구" and not three:
            ph, pa, hnote = _basketball_h_check(rows, league, ph, pa)
        elif sport == "배구" and not three:
            ph, pa, hnote = _volleyball_h_check(rows, ph, pa)
        ah, n_h = _adj(form, home); aa, n_a = _adj(form, away)
        sb, n_s = _adj(form, f"__sport:{sport}", k=80, cap=0.03)       # 종목 전체의 '홈 유리/불리' 편향(표본 많이 쌓여야 반영)
        ah, aa = ah + sb, aa - sb
        ph2, pa2 = ph + ah - aa * 0.5, pa + aa - ah * 0.5
        s = ph2 + px + pa2
        ph2, px, pa2 = ph2 / s, px / s, pa2 / s
        cal_n = None
        if sport == "축구" and three:                    # 해외 축구 CSV 로 만든 보정표 적용(있을 때만)
            ph2, px, pa2, cal_n = football_calib.calibrate(ph2, px, pa2)
        model = dict(cal_n=cal_n, total=total, ph=ph2, px=px, pa=pa2, ph0=ph_m, pa0=pa_m, hnote=hnote, three=three, adj=(ah, aa, n_h, n_a),
                     over=(1 / base["odds1"] + (1 / base["oddsx"] if three else 0) + 1 / base["odds2"]))
        if sport in ("축구", "야구"):
            lh, la = fit_poisson(ph2, px if three else None, pa2, total)
            model["lam"] = (lh, la)
        elif sport == "농구":
            sig = SIGMA.get(league, 12.0)
            z = NormalDist().inv_cdf(min(max(ph2 / (ph2 + pa2), 0.02), 0.98))
            model["mu"], model["sig"] = sig * z, sig
        elif sport == "배구":
            model["q"] = fit_set_prob(min(max(ph2 / (ph2 + pa2), 0.03), 0.97))

    for idx, r in enumerate(rows):
        bt = r["bet_type"]
        if bt != "SUM" and has_odds(r) and not odds_ok(r):
            res[idx] = dict(pick="검수 필요", score="-", prob=None, label="⚠️ 검수",
                            why="배당 숫자가 비정상(OCR 오인식 가능) → 예측하지 않음. 파일 검수 표에서 수정하세요.", upset=None)
            continue
        if not has_odds(r):
            res[idx] = dict(pick="배당 미발매", score="-", prob=None, label="⏳ 대기", why="국내 배당이 아직 없어 예측하지 않습니다.", upset=None)
            continue
        if is_margin_bet(bt):
            res[idx] = _predict_margin(r, model, sport, total, bt)
            continue
        if bt == "SUM":
            res[idx] = dict(pick="예측 제외", score="-", prob=None, label="⚪ 제외", why="홀짝은 통계적으로 50:50 에 가까워 예측 근거가 없습니다.", upset=None)
            continue
        three_row = r["oddsx"] is not None and not pd.isna(r["oddsx"])
        mk = devig([r["odds1"]] + ([r["oddsx"]] if three_row else []) + [r["odds2"]])
        payout = 100 / sum(1 / o for o in ([r["odds1"]] + ([r["oddsx"]] if three_row else []) + [r["odds2"]]))

        if bt in HALF_TYPES:                               # 전반 경기: 점수 모델이 없어 시장확률(마진 제거)만 사용
            names_h = [n for n in slot_names(bt, r["line"]) if n is not None]
            pick, pp = max(zip(names_h, mk), key=lambda x: x[1])
            res[idx] = dict(pick=pick, score="-", prob=pp, label=confidence_label(pp, 3 if three_row else 2),
                            why=f"전반 경기 시장확률(환급률 {payout:.1f}% 마진제거) — 전반 스코어 데이터가 없어 학습·점수예측은 하지 않음",
                            upset=min(mk[0], mk[-1]), dist=list(zip(names_h, mk)),
                            alt=next(f"{n} {p*100:.0f}%" for n, p in sorted(zip(names_h, mk), key=lambda x: -x[1]) if n != pick))
            continue
        if bt == "일반" and model:
            ph, px, pa = model["ph"], model["px"], model["pa"]
            opts = [("홈승", ph), ("무", px), ("원정승", pa)] if model["three"] else [("홈승", ph), ("원정승", pa)]
            pick, pp = max(opts, key=lambda x: x[1])
            if model["three"] and px >= max(ph, pa) - 0.015 and px >= 0.27:      # 무승부가 거의 같은 수준이면 무를 예상으로
                pick, pp = "무", px
            ranked = sorted(opts, key=lambda x: -x[1])
            alt = next(f"{n} {p*100:.0f}%" for n, p in ranked if n != pick)
            score = _best_score(sport, model, _cat_pred("일반", None, pick))
            adj = model["adj"]
            adj_txt = f" / 학습보정 홈{adj[0]*100:+.1f}%p·원정{adj[1]*100:+.1f}%p(표본 {adj[2]}·{adj[3]}경기)" if (adj[2] or adj[3]) else " / 누적 학습표본 없음(시장확률 그대로)"
            adj_txt += f" / 해외축구 {model['cal_n']:,}경기 보정표 적용" if model.get("cal_n") else ""
            hn = f" / {model['hnote']}" if model.get("hnote") else ""
            why = (hn[3:] + " / " if hn else "") + (f"환수율 {payout:.1f}% 마진제거 → 홈 {ph*100:.1f}%"
                   + (f" / 무 {px*100:.1f}%" if model["three"] else "") + f" / 원정 {pa*100:.1f}%" + adj_txt)
            up = min(ph, pa)
            tn = _trend_note(base, ph, pa)
            smap = {n: _best_score(sport, model, _cat_pred("일반", None, n)) for n, _ in opts}
            res[idx] = dict(pick=pick, score=score, prob=pp, label=confidence_label(pp, 3 if model["three"] else 2),
                            why=why, upset=up, dist=opts, alt=alt, score_map=smap,
                            draw=draw_level(px, ph, pa) if model["three"] else "", upset_txt=upset_level(up, ph, pa), signal=tn)
        elif bt == "H" and r["line"] is not None and model:
            line = r["line"]
            names = ["핸디승", "핸디무", "핸디패"] if three_row else ["핸디승", "핸디패"]
            mk_pick, mk_p = max(zip(names, mk), key=lambda x: x[1])
            model_p = _model_cover(sport, model, line, three_row)
            if model_p:
                blend = [0.5 * a + 0.5 * b for a, b in zip(mk, model_p)]
                s = sum(blend); blend = [x / s for x in blend]
            else:
                blend = mk
            pick, pp = max(zip(names, blend), key=lambda x: x[1])
            smap = {n: _best_score(sport, model, _cat_pred("H", line, n)) for n in names}
            score = smap[pick]
            gap = (model_p[0] - mk[0]) * 100 if model_p else 0
            why = (f"핸디 {line:+g} 배당 마진제거 {names[0]} {mk[0]*100:.1f}%"
                   + (f"/무 {mk[1]*100:.1f}%" if three_row else "") + f"/{names[-1]} {mk[-1]*100:.1f}%"
                   + (f" · 일반배당 기반 모델 커버확률 {model_p[0]*100:.1f}% (차이 {gap:+.1f}%p, 둘을 50:50 합성)" if model_p else ""))
            res[idx] = dict(pick=pick, score=score, prob=pp, label=confidence_label(pp, 3 if three_row else 2),
                            why=why, upset=min(blend[0], blend[-1]), dist=list(zip(names, blend)), score_map=smap,
                            alt=next(f"{n} {p*100:.0f}%" for n, p in sorted(zip(names, blend), key=lambda x: -x[1]) if n != pick))
        elif bt == "U":
            pu, po = mk[0], mk[-1]
            pick, pp = ("언더", pu) if pu >= po else ("오버", po)
            why = f"언더/오버 환수율 {payout:.1f}% 마진제거 → 언더 {pu*100:.1f}% / 오버 {po*100:.1f}% (기준 {r['line'] if r['line'] else '?'})"
            sc = ""
            if model and "lam" in model:
                sc = f"기대 총득점 {sum(model['lam']):.1f}"
            elif model and "mu" in model:
                sc = f"기대 총점 {total:.0f}"
            res[idx] = dict(pick=pick, score=sc or "-", prob=pp, label=confidence_label(pp, 2), why=why, upset=min(pu, po),
                            dist=[("언더", pu), ("오버", po)], alt=f"{'오버' if pick == '언더' else '언더'} {min(pu, po)*100:.0f}%")
        else:
            pick, pp = ("승", mk[0]) if mk[0] >= mk[-1] else ("패", mk[-1])
            res[idx] = dict(pick=pick, score="-", prob=pp, label=confidence_label(pp, 2),
                            why=f"마진제거 확률 {mk[0]*100:.1f}% / {mk[-1]*100:.1f}%", upset=min(mk[0], mk[-1]))
    lam_ = model.get("lam") if model else None                 # 점수 확률표(결합분포)를 만들 때 쓰는 득점 기대치
    for k_ in res:
        res[k_]["lam"] = lam_
    return res


def _vb_scores(q):
    """배구 5세트제 세트 스코어 분포. q = 홈이 한 세트를 이길 확률. {(홈세트, 원정세트): 확률}"""
    from math import comb
    out = {}
    for k in range(3):
        out[(3, k)] = comb(2 + k, k) * q ** 3 * (1 - q) ** k
        out[(k, 3)] = comb(2 + k, k) * (1 - q) ** 3 * q ** k
    return out


def _cat_pred(bt, line, name):
    """결과 이름 → '홈-원정 점수차(d)'가 그 결과에 해당하는지 판정하는 함수"""
    if bt == "H":
        return {"핸디승": lambda d: d + line > 1e-9, "핸디무": lambda d: abs(d + line) <= 1e-9, "핸디패": lambda d: d + line < -1e-9}[name]
    if is_margin_bet(bt):
        n = int(line)
        return {"승": lambda d: d > n, "패": lambda d: d < -n}.get(name, lambda d: abs(d) <= n)
    return {"홈승": lambda d: d > 0, "무": lambda d: d == 0, "원정승": lambda d: d < 0}[name]


def _best_score(sport, model, pred):
    """모델의 점수 분포에서 pred(점수차)를 만족하는 가장 그럴듯한 스코어. 모델이 없으면 '-'"""
    if not model:
        return "-"
    if sport in ("축구", "야구") and "lam" in model:
        M = _score_matrix(*model["lam"], n=16)
        best, bp = (0, 0), -1
        for i in range(16):
            for j in range(16):
                if sport == "야구" and i == j:
                    continue
                if pred(i - j) and M[i][j] > bp:
                    best, bp = (i, j), M[i][j]
        return f"{best[0]} : {best[1]}" if bp >= 0 else "-"
    if sport == "배구" and "q" in model:
        sc = {k: v for k, v in _vb_scores(model["q"]).items() if pred(k[0] - k[1])}
        if not sc:
            return "-"
        h, a = max(sc, key=sc.get)
        return f"{h} : {a}"
    if sport == "농구" and "mu" in model:
        cand = [m for m in range(-40, 41) if pred(m)]
        if not cand:
            return "-"
        m = min(cand, key=lambda x: abs(x - model["mu"]))
        T = model.get("total", DEFAULT_TOTAL["농구"])
        return f"{round((T + m) / 2)} : {round((T - m) / 2)}"
    return "-"


def handicap_legend(line):
    """핸디 정수 라인의 결과표(무엇이 핸디승/무/패인지)"""
    k = int(abs(line))
    if line < 0:
        return f"핸디{line:+g} 결과표: 홈 {k+1}점차 이상 승=핸디승 · 홈 정확히 {k}점차 승=핸디무 · 그 외(홈이 {k}점차 미만으로 이기거나 무·패)=핸디패"
    return f"핸디{line:+g} 결과표: 원정 {k+1}점차 이상 승=핸디패 · 원정 정확히 {k}점차 승=핸디무 · 그 외(홈 승·무, 원정 {k-1}점차 이하 승)=핸디승"


def _margin_probs(sport, model, n):
    """일반 배당에서 역산한 점수 분포로 [승(홈이 n+1점 이상 승), n(점수차 n점 이내), 패] 확률"""
    if not model:
        return None
    if sport in ("축구", "야구") and "lam" in model:
        M = _score_matrix(*model["lam"], n=16)
        w = b = l = 0.0
        for i in range(len(M)):
            for j in range(len(M)):
                d = i - j
                if d > n: w += M[i][j]
                elif d < -n: l += M[i][j]
                elif d == 0 and sport == "야구": continue     # 야구는 무승부 없음 → 제외하고 재정규화
                else: b += M[i][j]
        t = w + b + l
        return [w / t, b / t, l / t]
    if sport == "농구" and "mu" in model:
        nd = NormalDist(model["mu"], model["sig"])
        w = 1 - nd.cdf(n + 0.5)
        l = nd.cdf(-n - 0.5)
        return [w, 1 - w - l, l]
    return None


def _predict_margin(r, model, sport, total, bt):
    n = int(r["line"]) if r["line"] and not pd.isna(r["line"]) else int(re.search(r"\d+", bt).group())
    names = ["승", str(n), "패"]
    if pd.isna(r["oddsx"]) or not has_odds(r):
        return dict(pick="검수 필요", score="-", prob=None, label="⚠️ 검수", why="승N패는 3개 배당이 필요한데 읽히지 않았습니다.", upset=None)
    mk = devig([r["odds1"], r["oddsx"], r["odds2"]])
    mp = _margin_probs(sport, model, n)
    if mp:
        bl = [0.5 * a + 0.5 * b for a, b in zip(mk, mp)]
        s_ = sum(bl); bl = [x / s_ for x in bl]
    else:
        bl = mk
    pick, pp = max(zip(names, bl), key=lambda x: x[1])
    smap = {nm: _best_score(sport, model, _cat_pred(bt, float(n), nm)) for nm in names}
    score = smap[pick]
    why = (f"승{n}패 배당 마진제거 승 {mk[0]*100:.1f}% / {n} {mk[1]*100:.1f}% / 패 {mk[2]*100:.1f}%"
           + (f" · 일반배당 기반 점수분포 {mp[0]*100:.1f}/{mp[1]*100:.1f}/{mp[2]*100:.1f}% (50:50 합성)" if mp else " · 이 종목은 점수모델이 없어 시장확률만 사용"))
    return dict(pick=pick, score=score, prob=pp, label=confidence_label(pp, 3), why=why, upset=min(bl[0], bl[2]),
                dist=list(zip(names, bl)), score_map=smap, alt=next(f"{n} {p*100:.0f}%" for n, p in sorted(zip(names, bl), key=lambda x: -x[1]) if n != pick))


def _score_text(sport, model, pick):
    if sport in ("축구", "야구") and "lam" in model:
        lh, la = model["lam"]
        M = _score_matrix(lh, la)
        best, bp = (0, 0), -1
        for i in range(len(M)):
            for j in range(len(M)):
                ok = (pick == "홈승" and i > j) or (pick == "원정승" and i < j) or (pick == "무" and i == j)
                if ok and M[i][j] > bp:
                    best, bp = (i, j), M[i][j]
        return f"{best[0]} : {best[1]}"
    if sport == "농구" and "mu" in model:
        t = model.get("total", DEFAULT_TOTAL["농구"])
        mu = model["mu"]
        h, a = round((t + mu) / 2), round((t - mu) / 2)
        return f"{h} : {a}"
    if sport == "배구" and "q" in model:
        q = model["q"]
        c = {"3:0": q**3, "3:1": 3 * q**3 * (1 - q), "3:2": 6 * q**3 * (1 - q) ** 2}
        s = max(c, key=c.get)
        return s if pick == "홈승" else s[::-1]
    return "-"


def _model_cover(sport, model, line, three):
    """일반 배당에서 역산한 점수 분포로 '핸디 line 적용 시' 홈 승/무/패 확률"""
    if sport in ("축구", "야구") and "lam" in model:
        M = _score_matrix(*model["lam"])
        w = d = l = 0.0
        for i in range(len(M)):
            for j in range(len(M)):
                if sport == "야구" and i == j:
                    continue                                  # 야구는 무승부가 없으므로 제외하고 재정규화
                diff = i - j + line
                if diff > 1e-9: w += M[i][j]
                elif diff < -1e-9: l += M[i][j]
                else: d += M[i][j]
        return [w, d, l] if three else [w / (w + l), l / (w + l)]
    if sport == "농구" and "mu" in model:
        nd = NormalDist(model["mu"] + line, model["sig"])
        w = 1 - nd.cdf(0)
        return [w, 1 - w]
    if sport == "배구" and "q" in model:
        sc = _vb_scores(model["q"])
        w = sum(v for (h, a), v in sc.items() if (h - a) + line > 1e-9)
        return [w, 1 - w]
    return None


# ───────────────────── 저장된 과거 경기로 만드는 통계 ─────────────────────
def history_table(df_all):
    """끝난 경기(일반 행에 최종 스코어가 있는 경기)의 모든 행에 실제 결과·환급률·배당변동을 붙인 표"""
    if df_all.empty:
        return pd.DataFrame()
    games = {}
    for r in df_all.to_dict("records"):                       # 한 번만 훑으며 경기별로 묶는다(느린 groupby/필터 반복 제거)
        games.setdefault((r["match_datetime"], r["league"], r["home_team"], r["away_team"]), []).append(r)
    rows = []
    for g in games.values():
        base = next((r for r in g if r["bet_type"] == "일반"), None)
        if base is None or pd.isna(base["home_score"]):
            continue
        hs, as_ = int(base["home_score"]), int(base["away_score"])
        for r in g:
            if not has_odds(r) or not odds_ok(r):
                continue
            act = _actual(r, hs, as_)
            if act is None:
                continue
            names = slot_names(r["bet_type"], r["line"])
            od = [r["odds1"], r["oddsx"], r["odds2"]]
            avail = [i for i in range(3) if names[i] is not None and pd.notna(od[i])]
            if act not in names or len(avail) < 2:
                continue
            pr = devig([od[i] for i in avail])
            fav_i = avail[min(range(len(avail)), key=lambda j: od[avail[j]])]
            tr = [r.get("trend1"), r.get("trendx"), r.get("trend2")]
            tr = [0 if (t is None or pd.isna(t)) else int(t) for t in tr]
            rows.append(dict(sport=r["sport"], league=r["league"], bet_type=r["bet_type"], line=r["line"],
                             o1=r["odds1"], ox=r["oddsx"], o2=r["odds2"], actual=act,
                             op1=r.get("open1"), opx=r.get("openx"), op2=r.get("open2"), open_line=r.get("open_line"),
                             payout=1 / sum(1 / od[i] for i in avail), act_idx=names.index(act), fav_idx=fav_i,
                             fav_p=max(pr), t1=tr[0], tx=tr[1], t2=tr[2], match_datetime=r["match_datetime"]))
    out = pd.DataFrame(rows)
    if not out.empty:
        yr = out.match_datetime.str[:4].astype(int)
        out["w"] = 0.6 ** (yr.max() - yr)          # 최신 시즌 1.0, 한 시즌 전 0.6, 두 시즌 전 0.36 … (과거는 표본 확장용)
    return out


def _side_col(i):
    return ("o1", "ox", "o2")[i]


def similar_stats(hist, r, groups=None):
    """같은 종목·같은 유형(·같은 핸디라인)에서 '두 배당(홈·상대)이 모두 비슷했던' 과거 경기의 실제 결과 분포.
    return (경기수, {결과: 가중치합}, 가중치 총합)  — 최신 시즌 경기에 더 큰 가중치"""
    if hist is None or hist.empty or not has_odds(r):
        return 0, Counter(), 0.0
    h = groups.get((r["sport"], r["bet_type"])) if groups is not None else hist[(hist.sport == r["sport"]) & (hist.bet_type == r["bet_type"])]
    bt = r["bet_type"]
    if h is None or h.empty:
        return 0, Counter(), 0.0
    if (bt in ("H", "hH") or is_margin_bet(bt)) and pd.notna(r["line"]):
        h = h[(h.line - r["line"]).abs() < 1e-9]
    elif bt in ("U", "hU") and pd.notna(r["line"]):
        h = h[(h.line - r["line"]).abs() <= max(0.5, 0.03 * r["line"])]
    o1, o2 = r["odds1"], r["odds2"]
    h = h[((h.o1 - o1).abs() <= max(0.06, 0.04 * o1)) & ((h.o2 - o2).abs() <= max(0.06, 0.04 * o2))]
    if pd.notna(r["oddsx"]):
        h = h[h.ox.notna() & ((h.ox - r["oddsx"]).abs() <= max(0.1, 0.05 * r["oddsx"]))]
    if h.empty:
        return 0, Counter(), 0.0
    return len(h), Counter(h.groupby("actual").w.sum().to_dict()), float(h.w.sum())


def band_stats(hist, r, pick, groups=None):
    """'내가 고른 쪽의 배당대'(상대 배당은 무관)에서의 가중 적중률 — 표본이 더 넓다. return (경기수, 적중률, 배당, 가중치합)"""
    if hist is None or hist.empty or not has_odds(r):
        return 0, None, None, 0.0
    names = slot_names(r["bet_type"], r["line"])
    if pick not in names:
        return 0, None, None, 0.0
    i = names.index(pick)
    od = (r["odds1"], r["oddsx"], r["odds2"])[i]
    if pd.isna(od):
        return 0, None, None, 0.0
    h = groups.get((r["sport"], r["bet_type"])) if groups is not None else hist[(hist.sport == r["sport"]) & (hist.bet_type == r["bet_type"])]
    if h is None or h.empty:
        return 0, None, od, 0.0
    if (r["bet_type"] in ("H", "hH") or is_margin_bet(r["bet_type"])) and pd.notna(r["line"]):
        h = h[(h.line - r["line"]).abs() < 1e-9]
    h = h[(h[_side_col(i)] - od).abs() <= max(0.05, 0.08 * od)]
    if h.empty:
        return 0, None, od, 0.0
    W = float(h.w.sum())
    return len(h), float(((h.actual == pick) * h.w).sum() / W), od, W


def odds_winrate_table(hist):
    """종목별 '일반 배당이 가리키는 홈승 확률' 구간 vs 실제 홈승률"""
    if hist is None or hist.empty:
        return pd.DataFrame()
    h = hist[hist.bet_type == "일반"].copy()
    if h.empty:
        return pd.DataFrame()
    def imp(r):
        o = [r.o1] + ([r.ox] if pd.notna(r.ox) else []) + [r.o2]
        return devig(o)[0]
    h["implied"] = h.apply(imp, axis=1)
    h["구간"] = pd.cut(h.implied, [0, .35, .5, .65, .8, 1.01], right=False).astype(str)
    h["home"] = (h.actual == "홈승").astype(float)
    t = h.groupby(["sport", "구간"]).agg(경기수=("home", "size"), 배당이_가리킨_홈승=("implied", "mean"), 실제_홈승률=("home", "mean")).reset_index()
    d = t["실제_홈승률"] - t["배당이_가리킨_홈승"]
    t["판정"] = ["[표본 부족 - 참고용]" if n < 15 else ("⚠️ 함정 구간(배당보다 덜 이김)" if x <= -0.05 else ("💎 저평가 구간(배당보다 더 이김)" if x >= 0.05 else "정상"))
               for n, x in zip(t["경기수"], d)]
    for c in ("배당이_가리킨_홈승", "실제_홈승률"):
        t[c] = (t[c] * 100).round(1).astype(str) + "%"
    return t


def handicap_winrate_table(hist):
    """종목·핸디 라인별 핸디승/무/패 실제 비율"""
    if hist is None or hist.empty:
        return pd.DataFrame()
    h = hist[hist.bet_type == "H"].copy()
    if h.empty:
        return pd.DataFrame()
    g = h.groupby(["sport", "line"])
    t = g.agg(경기수=("actual", "size"),
              핸디승=("actual", lambda x: (x == "핸디승").mean()),
              핸디무=("actual", lambda x: (x == "핸디무").mean()),
              핸디패=("actual", lambda x: (x == "핸디패").mean())).reset_index().sort_values("경기수", ascending=False).head(40)
    for c in ("핸디승", "핸디무", "핸디패"):
        t[c] = (t[c] * 100).round(0).astype(int).astype(str) + "%"
    return t.rename(columns={"line": "핸디라인"})


def payout_table(hist):
    """환급률 구간별로 '인기(낮은 배당) 쪽'이 실제로 얼마나 이겼는지 vs 배당이 가리킨 확률"""
    if hist is None or hist.empty:
        return pd.DataFrame()
    h = hist.copy()
    h["유형"] = h.bet_type.map(type_group)
    h["환급률"] = pd.cut(h.payout * 100, [0, 84, 87, 90, 93, 200], right=False).astype(str)
    h["fav_hit"] = (h.act_idx == h.fav_idx).astype(float)
    t = h.groupby(["유형", "환급률"]).agg(경기수=("fav_hit", "size"), 인기측_적중=("fav_hit", "mean"), 배당이_가리킨=("fav_p", "mean")).reset_index()
    t["차이(%p)"] = ((t["인기측_적중"] - t["배당이_가리킨"]) * 100).round(1)
    for c in ("인기측_적중", "배당이_가리킨"):
        t[c] = (t[c] * 100).round(1).astype(str) + "%"
    return t


def trend_table(hist):
    """사이트가 표시한 배당 변동(↓하락/↑상승)이 붙은 쪽의 실제 적중률 vs 배당이 가리킨 확률"""
    if hist is None or hist.empty:
        return pd.DataFrame()
    recs = []
    for r in hist.itertuples():
        names = slot_names(r.bet_type, r.line)
        od = [r.o1, r.ox, r.o2]
        avail = [i for i in range(3) if names[i] is not None and pd.notna(od[i])]
        pr = dict(zip(avail, devig([od[i] for i in avail])))
        for i, tr in zip(range(3), (r.t1, r.tx, r.t2)):
            if i in pr and tr != 0:
                recs.append(dict(유형=("일반" if r.bet_type == "일반" else ("핸디" if r.bet_type == "H" else ("언더오버" if r.bet_type == "U" else "기타"))),
                                 변동="배당 하락↓" if tr < 0 else "배당 상승↑", hit=float(r.act_idx == i), p=pr[i]))
    if not recs:
        return pd.DataFrame()
    d = pd.DataFrame(recs)
    t = d.groupby(["유형", "변동"]).agg(건수=("hit", "size"), 실제_적중=("hit", "mean"), 배당이_가리킨=("p", "mean")).reset_index()
    t["차이(%p)"] = ((t["실제_적중"] - t["배당이_가리킨"]) * 100).round(1)
    for c in ("실제_적중", "배당이_가리킨"):
        t[c] = (t[c] * 100).round(1).astype(str) + "%"
    return t


def _renorm_dist(dist, pick, adj):
    out = [(n, p + adj) if n == pick else (n, p) for n, p in dist]
    rest = sum(p for n, p in out if n != pick)
    tgt = 1 - dict(out)[pick]
    return [(n, p) if n == pick else (n, p * tgt / rest if rest > 0 else p) for n, p in out]


def _selfcal_delta(table, grp, base_p):
    """앱이 내놓은 예측 확률(base)이 실제로는 얼마나 맞았는지(예측-실제 기록)로 만든 보정표. 표본 15건 이상인 구간만, 최대 ±4%p."""
    for lo, hi, mean_p, hit, n in (table or {}).get(grp, []):
        if lo <= base_p < hi and n >= 15:
            return max(-0.04, min(0.04, (hit - mean_p) * n / (n + 60))), n
    return 0.0, 0


def _repick(dist, bt):
    """보정이 끝난 확률 분포에서 '가장 높은 결과'를 예상으로(일반 3-way 는 무가 거의 같은 수준이면 무)"""
    d = dict(dist)
    pick = max(d, key=d.get)
    if bt == "일반" and "무" in d:
        ph, px, pa = d.get("홈승", 0.0), d["무"], d.get("원정승", 0.0)
        if px >= max(ph, pa) - 0.015 and px >= 0.27:
            pick = "무"
    return pick, d[pick]


def _norm(d):
    t = sum(d.values())
    return {k: v / t for k, v in d.items()} if t > 0 else d


def apply_history(res, hist, selfcal=None):
    """예측 표에 사용자의 과거 데이터 통계를 붙이고 확률을 보정한다. 모든 결과(홈승·무·원정승, 핸디승·무·패 …)를
    각자의 과거 실제 비율로 함께 보정한 뒤 예상(pick)·스코어·대안을 다시 고른다(막대와 예상이 어긋나지 않게).
    ① 두 배당이 모두 비슷했던 경기의 결과(표본 5+ → 결과별 최대 ±5%p) ② 그 결과의 배당대 적중률(표본 8+ → 최대 ±3%p)
    ③ 일반 경기는 '역배(이변)' 과거 비율로 이변 확률도 보정 ④ 앱의 과거 예측-실제 결과로 만든 자기보정(최대 ±4%p).
    과거 시즌은 가중치를 낮춰 반영(최신 1.0, 한 시즌 전 0.6, …)."""
    keys = ("pred_hist", "pred_prob", "pred_base", "pred_label", "pred_why", "pred_dist", "pred_upset", "pred_upset_txt",
            "pred_pick", "pred_score", "pred_alt", "pred_draw", "pred_sc")
    cols = {k: [] for k in keys}
    groups = {k: v for k, v in hist.groupby(["sport", "bet_type"])} if hist is not None and not hist.empty else None
    for _, r in res.iterrows():
        pick0, p0 = r["pred_pick"], r["pred_prob"]
        p, why, lines = p0, r["pred_why"], []
        dist, up, up_txt = r.get("pred_dist"), r.get("pred_upset"), r.get("pred_upset_txt")
        pick, score, alt, draw = pick0, r["pred_score"], r.get("pred_alt"), r.get("pred_draw")
        base_p = p0
        sc = 0.0
        if pd.notna(p0) and pick0 not in ("검수 필요", "배당 미발매", "예측 제외") and isinstance(dist, list):
            d0 = dict(dist)
            n, cnt, W = similar_stats(hist, r, groups)
            adjd = {}
            for name, pv in d0.items():
                adj = 0.0
                if n >= 5 and W > 0:
                    adj += max(-0.05, min(0.05, (cnt.get(name, 0) / W - pv) * W / (W + 40)))
                nb, rb, od, Wb = band_stats(hist, r, name, groups)
                if nb >= 8 and Wb > 0:
                    adj += max(-0.03, min(0.03, (rb - pv) * Wb / (Wb + 80)))
                adjd[name] = max(0.005, pv + max(-0.06, min(0.06, adj)))
            adjd = _norm(adjd)
            dist = [(nm, adjd[nm]) for nm, _ in dist]
            if n >= 5 and W > 0:
                top = " · ".join(f"{k} {v*100/W:.0f}%" for k, v in cnt.most_common(3))
                lines.append(f"📚 같은 배당 조합(홈·상대 모두 비슷) {n}경기: {top}")
                if r["bet_type"] == "일반" and pd.notna(up):
                    under = "원정승" if r["odds2"] > r["odds1"] else "홈승"
                    ur = cnt.get(under, 0) / W
                    up = up + max(-0.05, min(0.05, (ur - up) * W / (W + 40)))
                    lines.append(f"🎲 같은 배당 조합에서 역배({under}) 실제 {ur*100:.0f}% (배당 환산 {r['pred_upset']*100:.0f}%)")
            elif n > 0:
                lines.append(f"📚 같은 배당 조합 과거 {n}경기뿐 [표본 부족 - 참고용]")
            else:
                lines.append("📚 같은 배당 조합의 과거 기록 없음")
            pick, p = _repick(dist, r["bet_type"])
            nb, rb, od, Wb = band_stats(hist, r, pick, groups)
            if nb >= 8 and Wb > 0:
                lines.append(f"📊 이 배당대({od:g}±8%)에서 '{pick}' 실제 적중 {rb*100:.0f}% ({nb}경기)")
            elif nb > 0:
                lines.append(f"📊 이 배당대 과거 {nb}경기뿐 [표본 부족 - 참고용]")
            base_p = p                                              # 자기보정 '이전' 확률(예측 기록·보정표의 기준)
            sc, sn = _selfcal_delta(selfcal, type_group(r["bet_type"]), base_p)
            if sc:
                dist = list(_renorm_dist(dist, pick, sc))
                pick, p = _repick(dist, r["bet_type"])
                why += f" · 앱 예측기록 자기보정 {sc*100:+.1f}%p(과거 {sn}건)"
            delta = dict(dist)[pick] - d0.get(pick, dict(dist)[pick])
            if abs(delta) >= 0.0005:
                why += f" · 과거데이터 보정 {pick} {delta*100:+.1f}%p"
            if pick != pick0:
                why += f" · ⚠️ 과거 실제 결과를 반영하자 예상이 '{pick0}' → '{pick}' 로 바뀜"
            smap = r.get("pred_score_map")
            if isinstance(smap, dict) and pick in smap:
                score = smap[pick]
            ranked = sorted(dist, key=lambda x: -x[1])
            alt = next((f"{n_} {p_*100:.0f}%" for n_, p_ in ranked if n_ != pick), alt)
            if len(ranked) >= 2 and ranked[0][1] - ranked[1][1] < 0.03:
                alt += f" (⚖️ 1·2위 차이 {(ranked[0][1] - ranked[1][1])*100:.0f}%p — 박빙, 단일 선택 비추천)"
            dd = dict(dist)
            if r["bet_type"] == "일반":
                if "무" in dd:
                    draw = draw_level(dd["무"], dd.get("홈승", 0), dd.get("원정승", 0))
                if pd.notna(up):
                    up_txt = upset_level(up, dd.get("홈승", 0), dd.get("원정승", 0))
        nway = 3 if (pd.notna(r["oddsx"]) or is_margin_bet(r["bet_type"])) else 2
        keep = r["pred_label"] in ("⚠️ 검수", "⏳ 대기", "⚪ 제외")
        cols["pred_hist"].append("\n".join(lines)); cols["pred_prob"].append(p); cols["pred_base"].append(base_p); cols["pred_why"].append(why)
        cols["pred_label"].append(r["pred_label"] if keep or pd.isna(p) else confidence_label(p, nway))
        cols["pred_dist"].append(dist); cols["pred_upset"].append(up); cols["pred_upset_txt"].append(up_txt)
        cols["pred_pick"].append(pick); cols["pred_score"].append(score); cols["pred_alt"].append(alt); cols["pred_draw"].append(draw)
        cols["pred_sc"].append(sc)
    res = res.copy()
    for k, v in cols.items():
        res[k] = v
    return res


# ───────────────────── 한 경기 전체를 하나의 스코어 확률표로 맞추기(일관성) ─────────────────────
def recommend(dist, bt):
    """결과 분포에서 '단일 추천 / 복수 추천 / 박빙 보류' 문구. 3지선다에서 방향이 약하면 1위 + 무(가운데 결과)를 묶는다."""
    d = sorted(dist, key=lambda x: -x[1])
    n1, p1 = d[0]
    if len(d) == 2:
        n2, p2 = d[1]
        if p1 >= 0.55:
            return f"단일 추천: {n1} ({p1*100:.0f}%)"
        return f"⚖️ 박빙 — 단일 선택 보류 ({n1} {p1*100:.0f}% / {n2} {p2*100:.0f}%)"
    names = [n for n, _ in dist]
    mid, pm = names[1], dist[1][1]
    n2, p2 = d[1]
    if p1 >= 0.55 or (p1 >= 0.45 and p1 - p2 >= 0.15):
        return f"단일 추천: {n1} ({p1*100:.0f}%)"
    partner, pp = (n2, p2) if n1 == mid else (mid, pm)
    return f"복수 추천: {n1} + {partner} (합 {(p1+pp)*100:.0f}%)" + (" · 방향성 약함" if p1 < 0.40 else "")


def _row_partition(r, I, J):
    """한 줄(일반·핸디·언더오버·승N패)을 '스코어 칸 → 결과 이름' 분할로 바꾼다. return (이름 순서, {이름: 불리언 마스크})"""
    D, T = I - J, I + J
    bt, line, d = r["bet_type"], r["line"], r["pred_dist"]
    if not isinstance(d, list) or bt == "SUM" or bt in HALF_TYPES:
        return None
    names = [n for n, _ in d]
    if bt == "U":
        if pd.isna(line):
            return None
        return names, {"언더": T < line, "오버": T > line}
    if bt == "H":
        if pd.isna(line):
            return None
        return names, {n: _cat_pred("H", line, n)(D) for n in names}
    if is_margin_bet(bt):
        return names, {n: _cat_pred(bt, float(line), n)(D) for n in names}
    if bt == "일반":
        return names, {n: _cat_pred("일반", None, n)(D) for n in names}
    return None


def _joint(lam, sport, parts, n=16):
    """푸아송 점수분포에서 출발해, 각 줄의 (보정된) 결과 확률에 맞도록 반복 비례조정(IPF)한 '한 경기의 스코어 확률표'.
    일반·핸디·언더오버가 서로 모순되지 않는 하나의 분포가 된다. 줄끼리 어긋나는 부분은 절충(감쇠 0.6)한다."""
    import numpy as np
    M = np.array(_score_matrix(lam[0], lam[1], n), dtype=float)
    if sport == "야구":
        np.fill_diagonal(M, 0.0)
    M /= M.sum()
    for _ in range(60):
        for masks, tgt in parts:
            for lab, mask in masks.items():
                cur, t = M[mask].sum(), tgt.get(lab)
                if t is not None and cur > 1e-12:
                    M[mask] *= (t / cur) ** 0.6
            M /= M.sum()
    return M


def _tn(bt, line):
    return {"일반": "일반", "H": f"핸디{line:+g}", "U": f"언더오버{line:g}"}.get(bt, bt if not is_margin_bet(bt) else f"승{int(line)}패")


def coherence_pass(res):
    """① 모든 줄에 단일/복수 추천 문구 ② 축구·야구는 한 경기를 하나의 스코어 확률표로 묶어
    줄별 확률·예상·스코어를 서로 일관되게 다시 계산하고, 시나리오 표·연결 추천·모순 경고를 만든다."""
    import numpy as np
    if res.empty:
        return res
    res = res.copy().reset_index(drop=True)
    n = len(res)
    names_ = ("pred_dist", "pred_pick", "pred_prob", "pred_score", "pred_alt", "pred_draw", "pred_upset", "pred_upset_txt", "pred_label", "pred_base")
    cols = {c: list(res[c]) for c in names_}
    for c in ("pred_rec", "pred_summary", "pred_scen", "pred_conflict", "pred_link"):
        cols[c] = [None] * n
    NG = 16
    I, J = np.indices((NG, NG))
    for _, g in res.groupby(["match_datetime", "league", "home_team", "away_team"], sort=False):
        pos = list(g.index)
        for p in pos:
            d = cols["pred_dist"][p]
            if isinstance(d, list) and len(d) >= 2 and cols["pred_pick"][p] not in ("검수 필요", "배당 미발매", "예측 제외"):
                cols["pred_rec"][p] = recommend(d, res.at[p, "bet_type"])
        sport = res.at[pos[0], "sport"]
        if sport not in ("축구", "야구"):
            continue
        lam = next((res.at[p, "pred_lam"] for p in pos if isinstance(res.at[p, "pred_lam"], (tuple, list))), None)
        if lam is None:
            continue
        parts, meta = [], []
        for p in pos:
            r = res.loc[p].to_dict()
            r["pred_dist"] = cols["pred_dist"][p]
            rp = _row_partition(r, I, J)
            if rp is None:
                continue
            nm, masks = rp
            tot = sum(v for _, v in r["pred_dist"])
            parts.append((masks, {k: v / tot for k, v in r["pred_dist"]}))
            meta.append((p, r["bet_type"], r["line"], nm, masks))
        if not any(m[1] == "일반" for m in meta):
            continue
        M = _joint(lam, sport, parts, NG)
        E_tot = float((M * (I + J)).sum())

        def mode(mask):
            sub = np.where(mask, M, -1.0)
            i, j = np.unravel_index(int(sub.argmax()), sub.shape)
            return f"{i} : {j}", float(M[i, j])

        # ── 줄별 재계산(모두 같은 스코어 확률표의 주변분포) ──
        picks = {}
        for p, bt, line, nm, masks in meta:
            dist = [(k, float(M[masks[k]].sum())) for k in nm]
            t = sum(v for _, v in dist)
            dist = [(k, v / t) for k, v in dist]
            old_pick = cols["pred_pick"][p]
            pick, pp = _repick(dist, bt)
            sc = res.at[p, "pred_sc"] if "pred_sc" in res.columns and pd.notna(res.at[p, "pred_sc"]) else 0.0
            cols["pred_dist"][p], cols["pred_pick"][p], cols["pred_prob"][p] = dist, pick, pp
            cols["pred_base"][p] = pp - (sc if pick == old_pick else 0.0)
            cols["pred_label"][p] = confidence_label(pp, 3 if len(dist) == 3 else 2)
            ranked = sorted(dist, key=lambda x: -x[1])
            alt = next((f"{a} {b*100:.0f}%" for a, b in ranked if a != pick), None)
            if len(ranked) >= 2 and ranked[0][1] - ranked[1][1] < 0.03 and alt:
                alt += f" (⚖️ 1·2위 차이 {(ranked[0][1]-ranked[1][1])*100:.0f}%p — 박빙)"
            cols["pred_alt"][p] = alt
            sc_txt, _pb = mode(masks[pick])
            cols["pred_score"][p] = f"{sc_txt} · 기대 총득점 {E_tot:.1f}" if bt == "U" else sc_txt
            cols["pred_rec"][p] = recommend(dist, bt)
            if bt == "일반":
                dd = dict(dist)
                if "무" in dd:
                    cols["pred_draw"][p] = draw_level(dd["무"], dd.get("홈승", 0), dd.get("원정승", 0))
                cols["pred_upset"][p] = min(dd.get("홈승", 0), dd.get("원정승", 0))
                cols["pred_upset_txt"][p] = upset_level(cols["pred_upset"][p], dd.get("홈승", 0), dd.get("원정승", 0))
            picks[p] = (bt, line, pick, masks)
        # ── 모순 검사: 줄별 1위 추천이 동시에 성립할 확률 ──
        allm = np.ones((NG, NG), dtype=bool)
        for bt, line, pick, masks in picks.values():
            allm &= masks[pick]
        joint_hit = float(M[allm].sum())
        # ── 시나리오 표(가장 그럴듯한 스코어 + 그 스코어에서 각 줄의 결과) ──
        flat = np.argsort(-M, axis=None)[:6]
        scen = []
        for f in flat:
            i, j = divmod(int(f), NG)
            labs = []
            for p, bt, line, nm, masks in meta:
                for k in nm:
                    if masks[k][i, j]:
                        labs.append(f"{_tn(bt, line)} {k}")
                        break
            scen.append(dict(score=f"{i} : {j}", p=float(M[i, j]), labs=labs))
        # ── 결과별 연결 시나리오: '일반이 이 결과라면 핸디·언더오버는?' ──
        base = next(m for m in meta if m[1] == "일반")
        others = [m for m in meta if m[1] != "일반"]
        lines = []
        rec_main = cols["pred_rec"][base[0]]
        lines.append("📌 종합 의견: " + rec_main)
        top_sc = " · ".join(f"{s['score']}({s['p']*100:.0f}%)" for s in scen[:4])
        lines.append(f"🎯 가장 그럴듯한 스코어: {top_sc} · 기대 총득점 {E_tot:.1f}")
        for k in base[3]:
            mo = base[4][k]
            po = float(M[mo].sum())
            if po < 0.05:
                continue
            seg = [f"{k} {po*100:.0f}% → 대표 스코어 {mode(mo)[0]}"]
            for p, bt, line, nm, masks in others:
                cond = {kk: float(M[mo & masks[kk]].sum()) / max(po, 1e-12) for kk in nm}
                bk = max(cond, key=cond.get)
                seg.append(f"{_tn(bt, line)} {bk} {cond[bk]*100:.0f}%")
            lines.append("   · " + " · ".join(seg))
        # ── 연결 추천(일반 1위를 기준으로 핸디·언더오버를 그 안에서 고름) ──
        top_o = cols["pred_pick"][base[0]]
        cur = base[4][top_o].copy()
        chain = [f"일반 {top_o}"]
        for p, bt, line, nm, masks in sorted(others, key=lambda m: (m[1] != "H", abs(m[2]) if pd.notna(m[2]) else 0)):
            best = max(nm, key=lambda kk: float(M[cur & masks[kk]].sum()))
            if float(M[cur & masks[best]].sum()) > 1e-9:
                cur &= masks[best]
                chain.append(f"{_tn(bt, line)} {best}")
        mo_top = base[4][top_o]                                   # 각 줄에 '일반 1위 결과라면' 기준의 조건부 추천을 붙인다
        for p, bt, line, nm, masks in others:
            den = float(M[mo_top].sum())
            if den > 1e-9:
                cond = {kk: float(M[mo_top & masks[kk]].sum()) / den for kk in nm}
                bk = max(cond, key=cond.get)
                cols["pred_link"][p] = f"일반 {top_o}이라면 → {bk} (조건부 {cond[bk]*100:.0f}%)"
        lines.append(f"🔗 연결 추천(서로 모순 없는 조합): {' → '.join(chain)} · 동시 적중확률 {float(M[cur].sum())*100:.0f}% · 대표 스코어 {mode(cur)[0]}")
        conflict = None
        if joint_hit < 0.03 and len(picks) >= 2:
            conflict = (f"⚠️ 줄별 1위 추천은 서로 동시에 성립하기 어렵습니다(동시 적중확률 {joint_hit*100:.1f}%). "
                        "각 줄은 '그 줄만의 확률'이 가장 큰 결과이므로, 한 경기를 여러 줄로 걸 때는 위 '연결 추천'이나 복수 추천을 기준으로 고르세요.")
            lines.append(conflict)
        for p in pos:
            cols["pred_summary"][p] = "\n".join(lines)
            cols["pred_scen"][p] = scen
            cols["pred_conflict"][p] = conflict
    for c, v in cols.items():
        res[c] = v
    return res


def build_predictions(df_all, target_df, hist=None, selfcal=None):
    """target_df 의 모든 행에 대해 예측 (학습: df_all 중 이미 종료된 경기)"""
    if target_df.empty:
        return pd.DataFrame()
    learn = df_all[pd.to_datetime(df_all.match_datetime) < now_kst()]
    form = team_form(learn)
    out = []
    keys = ["match_datetime", "league", "home_team", "away_team"]
    for _, g in target_df.groupby(keys, sort=False):
        rows = g.to_dict("records")
        preds = predict_game(rows, form)
        for i, r in enumerate(rows):
            out.append({**r, **{f"pred_{k}": v for k, v in preds[i].items()}})
    res = pd.DataFrame(out).sort_values(["match_datetime", "game_no"]).reset_index(drop=True)
    for c in ("pred_dist", "pred_alt", "pred_draw", "pred_upset_txt", "pred_signal", "pred_upset", "pred_score_map"):
        if c not in res.columns:
            res[c] = None
    return coherence_pass(apply_history(res, hist if hist is not None else history_table(learn), selfcal))


# ───────────────────── 조합 추천 ─────────────────────
def leg_odds(r):
    """예측(pick)한 쪽의 국내 배당"""
    names = slot_names(r["bet_type"], r["line"])
    if r["pred_pick"] not in names:
        return None
    v = (r["odds1"], r["oddsx"], r["odds2"])[names.index(r["pred_pick"])]
    return None if pd.isna(v) else float(v)


STYLES = {
    "안전형": dict(desc="적중 확률이 높은 순", min_p=0.55, key=lambda p, o: p),
    "균형형": dict(desc="확률×배당(기대환급)이 높은 순 · 확률 45% 이상", min_p=0.45, key=lambda p, o: p * o),
    "고배당형": dict(desc="배당 1.8 이상 중 확률×배당이 높은 순", min_p=0.30, key=lambda p, o: p * o if o >= 1.8 else 0),
}


def build_combos(pr, n_legs=4, style="안전형"):
    """한 경기에서는 한 가지 선택만, 서로 다른 경기 n_legs 개로 조합. 확률은 경기끼리 독립이라고 가정한 값."""
    if pr is None or pr.empty:
        return None
    cfg = STYLES[style]
    legs = []
    for r in pr.to_dict("records"):
        if pd.isna(r["pred_prob"]) or r["pred_pick"] in ("검수 필요", "배당 미발매", "예측 제외") or r["bet_type"] == "SUM":
            continue
        o = leg_odds(r)
        if not o or r["pred_prob"] < cfg["min_p"]:
            continue
        score = cfg["key"](r["pred_prob"], o)
        if score <= 0:
            continue
        legs.append(dict(r, _odds=o, _score=score, _game=(r["match_datetime"], r["league"], r["home_team"], r["away_team"])))
    best = {}
    for l in legs:
        if l["_game"] not in best or l["_score"] > best[l["_game"]]["_score"]:
            best[l["_game"]] = l
    chosen = sorted(best.values(), key=lambda x: -x["_score"])[:n_legs]
    if len(chosen) < 2:
        return None
    jp, jo = 1.0, 1.0
    for l in chosen:
        jp *= l["pred_prob"]; jo *= l["_odds"]
    return dict(legs=chosen, joint_p=jp, joint_odds=jo, ev=jp * jo)


def _actual(r, hs, as_):
    bt, line = r["bet_type"], r["line"]
    d = hs - as_
    if bt == "일반":
        three = pd.notna(r["oddsx"])
        return "홈승" if d > 0 else ("원정승" if d < 0 else ("무" if three else None))
    if bt == "H" and pd.notna(line):
        v = d + line
        if v > 1e-9: return "핸디승"
        if v < -1e-9: return "핸디패"
        return "핸디무" if pd.notna(r["oddsx"]) else None
    tot = r.get("total_score") if hasattr(r, "get") else None
    tot = hs + as_ if tot is None or pd.isna(tot) else tot          # 배구: 총 점수(행에 적힌 값) / 그 외: 스코어 합
    if bt == "U" and pd.notna(line):
        return "오버" if tot > line else ("언더" if tot < line else None)
    if bt == "SUM":
        return "홀" if int(tot) % 2 else "짝"
    if is_margin_bet(bt):
        n = int(line)
        return "승" if d > n else ("패" if d < -n else str(n))
    return None


def slot_names(bt, line):
    """배당 3칸(odds1/oddsx/odds2)이 각각 무엇을 뜻하는지"""
    if is_margin_bet(bt):
        n = str(int(line)) if line is not None and not pd.isna(line) else "5"
        return ("승", n, "패")
    return {"일반": ("홈승", "무", "원정승"), "H": ("핸디승", "핸디무", "핸디패"),
            "U": ("언더", None, "오버"), "SUM": ("홀", None, "짝"),
            "h": ("전반 홈승", "전반 무", "전반 원정승"), "hH": ("전반 핸디승", "전반 핸디무", "전반 핸디패"),
            "hU": ("전반 언더", None, "전반 오버")}.get(bt, ("1", "X", "2"))


def evaluate_finished(df_all, limit=300):
    """끝난 경기마다 '그 경기 이전 데이터만'으로 예측을 다시 만들어 실제 결과와 비교(미래 정보 누수 없음)."""
    keys = ["match_datetime", "league", "home_team", "away_team"]
    games = []
    for k, g in df_all.groupby(keys, sort=False):
        base = g[g.bet_type == "일반"]
        if base.empty or pd.isna(base.iloc[0]["home_score"]):
            continue
        games.append((k[0], g.to_dict("records"), int(base.iloc[0]["home_score"]), int(base.iloc[0]["away_score"])))
    games.sort(key=lambda x: x[0])
    games = games[-limit:]
    acc, out, i = {}, [], 0
    recs_by_time = {}
    for r in df_all[df_all.home_score.notna()].to_dict("records"):
        recs_by_time.setdefault(r["match_datetime"], []).append(r)
    times = sorted(recs_by_time)
    ti = 0
    for t, rows, hs, as_ in games:
        while ti < len(times) and times[ti] < t:           # 이 경기 시작 전에 끝난 경기들만 누적
            for r in recs_by_time[times[ti]]:
                for k, v in _resid_records(r):
                    a = acc.setdefault(k, [0.0, 0]); a[0] += v; a[1] += 1
            ti += 1
        home, away, sport = rows[0]["home_team"], rows[0]["away_team"], rows[0]["sport"]
        form = {k: (acc[k][0] / acc[k][1], acc[k][1]) for k in (home, away, f"__sport:{sport}") if k in acc}
        preds = predict_game(rows, form)
        for j, r in enumerate(rows):
            p, act = preds[j], _actual(r, hs, as_)
            if act is None or p["prob"] is None:
                continue
            grp = type_group(r["bet_type"])
            out.append(dict(sport=sport, 유형=grp, 라벨=p["label"], 예상확률=p["prob"], 적중=p["pick"] == act))
    return pd.DataFrame(out), len(games)


# ───────────────────── 배당 변화 · 접전 통계 ─────────────────────
def movement_table(hist):
    """내가 올린 파일에서 관측한 '처음 배당 → 마감 배당' 변화가 있었던 쪽의 실제 적중률 vs 마감 배당이 가리킨 확률"""
    if hist is None or hist.empty or "op1" not in hist.columns:
        return pd.DataFrame()
    recs = []
    for r in hist.itertuples():
        if pd.isna(r.op1) or (hasattr(r, "open_line") and pd.notna(r.open_line)):
            continue
        names = slot_names(r.bet_type, r.line)
        cur, opn = [r.o1, r.ox, r.o2], [r.op1, r.opx, r.op2]
        avail = [i for i in range(3) if names[i] is not None and pd.notna(cur[i]) and pd.notna(opn[i]) and opn[i] > 0]
        if len(avail) < 2:
            continue
        pr = dict(zip(avail, devig([cur[i] for i in avail])))
        for i in avail:
            chg = (cur[i] - opn[i]) / opn[i]
            b = ("크게 하락(-5%↓)" if chg <= -0.05 else "하락(-5~-1.5%)" if chg <= -0.015 else "변동 없음" if chg < 0.015 else "상승(+1.5~5%)" if chg < 0.05 else "크게 상승(+5%↑)")
            recs.append(dict(유형=type_group(r.bet_type), 변화=b, hit=float(r.act_idx == i), p=pr[i]))
    if not recs:
        return pd.DataFrame()
    d = pd.DataFrame(recs)
    t = d.groupby(["유형", "변화"]).agg(건수=("hit", "size"), 실제_적중=("hit", "mean"), 마감배당이_가리킨=("p", "mean")).reset_index()
    t["차이(%p)"] = ((t["실제_적중"] - t["마감배당이_가리킨"]) * 100).round(1)
    t["판정"] = ["[표본 부족 - 참고용]" if n < 15 else "" for n in t["건수"]]
    for c in ("실제_적중", "마감배당이_가리킨"):
        t[c] = (t[c] * 100).round(1).astype(str) + "%"
    return t


def closegame_table(df_all):
    """종목별·인기(낮은 배당) 구간별: 접전 발생률(축구 1점차 이내·무, 농구 5점 이내, 배구 풀세트, 야구 1점차)과 인기측 승률.
    핸디 신호(핸디 기준점 축소 또는 핸디 역배 쪽 배당 하락)가 있었던 경기와 없었던 경기를 나눠 비교."""
    if df_all.empty:
        return pd.DataFrame()
    base = df_all[(df_all.bet_type == "일반") & df_all.home_score.notna() & df_all.odds1.notna() & df_all.odds2.notna()].copy()
    if base.empty:
        return pd.DataFrame()
    # 핸디 신호: 같은 경기의 핸디(H) 줄 중 '기준점 축소' 또는 '배당이 높은 쪽 배당 하락'이 관측된 경우
    sig = set()
    for r in df_all[(df_all.bet_type == "H") & (df_all.n_chg > 0)].to_dict("records"):
        shrink = pd.notna(r.get("open_line")) and abs(r["line"]) < abs(r["open_line"])
        dog_drop = False
        if pd.notna(r.get("open1")) and pd.isna(r.get("open_line")):
            dog_i = 1 if r["odds2"] > r["odds1"] else 0
            cur, op = (r["odds1"], r["odds2"])[dog_i], (r["open1"], r["open2"])[dog_i]
            dog_drop = pd.notna(op) and op > 0 and (cur - op) / op <= -0.03
        if shrink or dog_drop:
            sig.add((r["match_datetime"], r["league"], r["home_team"], r["away_team"]))
    base["fav_odds"] = base[["odds1", "odds2"]].min(axis=1)
    base["fav_home"] = base.odds1 <= base.odds2
    d = (base.home_score - base.away_score).astype(float)
    base["fav_win"] = ((d > 0) & base.fav_home) | ((d < 0) & ~base.fav_home)
    def close(r):
        diff = abs(r.home_score - r.away_score)
        return {"축구": diff <= 1, "야구": diff <= 1, "농구": diff <= 5, "배구": (r.home_score + r.away_score) == 5}.get(r.sport, False)
    base["접전"] = base.apply(close, axis=1)
    base["무"] = (d == 0) & (base.sport == "축구")
    base["인기구간"] = pd.cut(base.fav_odds, [0, 1.3, 1.6, 2.0, 99], right=False).astype(str)
    base["핸디신호"] = [("있음" if (r.match_datetime, r.league, r.home_team, r.away_team) in sig else "없음") for r in base.itertuples()]
    t = base.groupby(["sport", "인기구간", "핸디신호"]).agg(경기수=("접전", "size"), 접전비율=("접전", "mean"), 무승부=("무", "mean"), 인기측승률=("fav_win", "mean")).reset_index()
    t["판정"] = ["[표본 부족 - 참고용]" if n < 15 else "" for n in t["경기수"]]
    for c in ("접전비율", "무승부", "인기측승률"):
        t[c] = (t[c] * 100).round(0).astype(int).astype(str) + "%"
    return t.sort_values(["sport", "인기구간", "핸디신호"])


# ───────────────────── 앱 예측 기록 → 실제 결과 비교 → 자기 보정 ─────────────────────
_last_push = {"t": 0.0}


def _sync_db_throttled(min_gap=300):
    import time, threading
    if time.time() - _last_push["t"] >= min_gap:
        _last_push["t"] = time.time()
        threading.Thread(target=_sync_db, daemon=True).start()


def log_predictions(pr):
    """아직 시작 안 한 경기의 예측(고른 쪽·확률)을 기록. 같은 줄은 가장 최근 예측으로 갱신되고, 경기가 시작되면 더 이상 바뀌지 않는다."""
    if pr is None or pr.empty or "row_key" not in pr.columns:
        return 0
    ts = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    ok = pr[pr.pred_prob.notna() & ~pr.pred_pick.isin(["검수 필요", "배당 미발매", "예측 제외"]) & (pr.bet_type != "SUM")]
    rows = [(r.row_key, ts, r.bet_type, r.sport, r.pred_pick, float(r.pred_base), float(r.pred_prob), r.pred_label,
             _nz(r.odds1), _nz(r.oddsx), _nz(r.odds2)) for r in ok.itertuples()]
    if not rows:
        return 0
    with _conn() as c:
        c.executemany("INSERT OR REPLACE INTO pred_log VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    _sync_db_throttled()
    return len(rows)


def eval_pred_log(df_all):
    """기록해 둔 예측 vs 실제 결과(스코어가 들어온 경기만)"""
    with _conn() as c:
        log = pd.read_sql("SELECT * FROM pred_log", c)
    if log.empty or df_all.empty:
        return pd.DataFrame()
    base = df_all[(df_all.bet_type == "일반") & df_all.home_score.notna()]
    score = {(r["match_datetime"], r["league"], r["home_team"], r["away_team"]): (int(r["home_score"]), int(r["away_score"])) for r in base.to_dict("records")}
    byk = df_all.set_index("row_key")
    out = []
    for lg in log.to_dict("records"):
        if lg["row_key"] not in byk.index:
            continue
        r = byk.loc[lg["row_key"]]
        r = r.iloc[-1] if isinstance(r, pd.DataFrame) else r
        sc = score.get((r["match_datetime"], r["league"], r["home_team"], r["away_team"]))
        if not sc:
            continue
        rd = r.to_dict()
        act = _actual(rd, sc[0], sc[1])
        if act is None:
            continue
        out.append(dict(row_key=lg["row_key"], match_datetime=r["match_datetime"], league=r["league"], sport=r["sport"],
                        경기=f"{r['home_team']} vs {r['away_team']}", bet_type=r["bet_type"], 유형=type_group(r["bet_type"]),
                        line=r["line"], pick=lg["pred_pick"], actual=act, base_prob=lg["base_prob"], pred_prob=lg["pred_prob"],
                        label=lg["pred_label"], hit=lg["pred_pick"] == act, score=f"{sc[0]}:{sc[1]}"))
    return pd.DataFrame(out)


def selfcal_table(ev):
    """유형별로 '앱이 낸 확률 구간 → 실제 적중률' 표 {유형: [(lo, hi, 평균예측, 실제적중, n)]}"""
    if ev is None or ev.empty:
        return {}
    bins = [0, .4, .5, .6, .7, .8, 1.01]
    ev = ev.copy()
    ev["bin"] = pd.cut(ev.base_prob, bins, right=False)
    tbl = {}
    for (grp, b), g in ev.groupby(["유형", "bin"], observed=True):
        tbl.setdefault(grp, []).append((float(b.left), float(b.right), float(g.base_prob.mean()), float(g.hit.mean()), int(len(g))))
    return tbl


def miss_review(ev, limit=30):
    """틀린 예측에 추정 원인 분류: [배당 구간 괴리 / 핸디캡 신호 / 라인업·부상 / 연전·체력 / 기타].
    라인업·연전은 이 프로그램이 알 수 없으므로 '확인 필요'로만 표시합니다."""
    if ev is None or ev.empty:
        return pd.DataFrame()
    m = ev[~ev.hit].sort_values("match_datetime", ascending=False).head(limit).copy()
    def cause(r):
        if r["유형"] == "일반" and r["actual"] == "무":
            return "핸디캡 신호(무 가능성 — 핸디 ±1 무 확률 확인)"
        if r["유형"] in ("핸디", "승N패", "전반핸디"):
            return "핸디캡 신호(일반·핸디 배당 어긋남)"
        if r["pred_prob"] >= 0.60:
            return "배당 구간 괴리(인기측 이변)"
        return "기타 — 라인업·부상·연전 여부 확인 필요"
    m["추정 원인"] = m.apply(cause, axis=1)
    m["예측확률"] = (m.pred_prob * 100).round(0).astype(int).astype(str) + "%"
    return m[["match_datetime", "경기", "유형", "pick", "예측확률", "actual", "score", "추정 원인"]].rename(
        columns={"match_datetime": "시각", "pick": "예측", "actual": "실제", "score": "스코어"})


# ───────────────────── 조합 찾기(게임 수·총배당·확률·역배 수 선택) ─────────────────────
def is_dog(r):
    """고른 쪽이 그 줄에서 가장 낮은 배당(인기)이 아니면 역배"""
    names = slot_names(r["bet_type"], r["line"])
    od = [r["odds1"], r["oddsx"], r["odds2"]]
    avail = [i for i in range(3) if names[i] is not None and pd.notna(od[i])]
    if r["pred_pick"] not in names or len(avail) < 2:
        return False
    fav = min(avail, key=lambda i: od[i])
    pi = names.index(r["pred_pick"])
    return pi != fav and od[pi] >= od[fav] * 1.08          # 배당 차이가 8% 미만인 팽팽한 줄은 '역배'로 보지 않는다


def find_combos(pr, n_legs=4, min_p=0.55, dog_legs=None, dog_min_p=0.25, total_odds=(1.0, 1e9), types=None,
                sports=None, day=None, rank="기대환급", top=5):
    """조건에 맞는 조합 상위 N개. dog_legs=None 이면 역배 수 상관없음, 정수면 정확히 그 개수.
    한 경기에서는 한 가지 선택만, 확률은 경기끼리 독립이라고 가정."""
    from itertools import combinations
    if pr is None or pr.empty:
        return [], 0
    types = types or ["일반", "H", "U"]
    cands = []
    for r in pr.to_dict("records"):
        bt = r["bet_type"]
        if pd.isna(r["pred_prob"]) or r["pred_pick"] in ("검수 필요", "배당 미발매", "예측 제외") or bt == "SUM":
            continue
        grp = "승N패" if is_margin_bet(bt) else bt
        if grp not in types or (sports and r["sport"] not in sports) or (day and not str(r["match_datetime"]).startswith(day)):
            continue
        o = leg_odds(r)
        if not o:
            continue
        dog = is_dog(r)
        if r["pred_prob"] < (dog_min_p if dog else min_p):
            continue
        cands.append(dict(r, _odds=o, _dog=dog, _game=(r["match_datetime"], r["league"], r["home_team"], r["away_team"]),
                          _ev=r["pred_prob"] * o))
    if not cands:
        return [], 0
    keyf = {"기대환급": lambda c: c["_ev"], "적중확률": lambda c: c["pred_prob"], "합산배당": lambda c: c["_odds"]}[rank]
    per_game = {}
    for c in sorted(cands, key=keyf, reverse=True):               # 한 경기당 후보 최대 3개
        per_game.setdefault(c["_game"], [])
        if len(per_game[c["_game"]]) < 3:
            per_game[c["_game"]].append(c)
    pool = [c for v in per_game.values() for c in v]
    K = 18 if n_legs <= 5 else 15
    main = sorted([c for c in pool if not c["_dog"]], key=keyf, reverse=True)[:K]
    dogs = sorted([c for c in pool if c["_dog"]], key=lambda c: c["_ev"], reverse=True)[:8] if dog_legs != 0 else []
    pool = main + [c for c in dogs if c not in main]
    results, tested = [], 0
    for combo in combinations(pool, n_legs):
        tested += 1
        if len({c["_game"] for c in combo}) < n_legs:
            continue
        nd = sum(c["_dog"] for c in combo)
        if dog_legs is not None and nd != dog_legs:
            continue
        jo = 1.0
        for c in combo:
            jo *= c["_odds"]
        if not (total_odds[0] <= jo <= total_odds[1]):
            continue
        jp = 1.0
        for c in combo:
            jp *= c["pred_prob"]
        results.append(dict(legs=list(combo), joint_p=jp, joint_odds=jo, ev=jp * jo, n_dog=nd))
    sk = {"기대환급": "ev", "적중확률": "joint_p", "합산배당": "joint_odds"}[rank]
    results.sort(key=lambda x: -x[sk])
    return results[:top], tested


# ───────────────────── 경기 메모 · master CSV ─────────────────────
def game_key(r):
    return f"{r['match_datetime']}|{r['league']}|{r['home_team']}|{r['away_team']}"


def save_note(gkey, memo):
    init_sports_db()
    with _conn() as c:
        if memo.strip():
            c.execute("INSERT OR REPLACE INTO game_notes VALUES(?,?,?)", (gkey, memo.strip(), now_kst().strftime("%Y-%m-%d %H:%M")))
        else:
            c.execute("DELETE FROM game_notes WHERE game_key=?", (gkey,))
    _sync_db()


def load_notes():
    init_sports_db()
    with _conn() as c:
        return {k: m for k, m in c.execute("SELECT game_key, memo FROM game_notes")}


def export_master():
    """누적 데이터를 시간 오름차순·중복 제거(날짜+리그+홈+원정+유형+기준값)해 master_games.csv 로 저장하고 R2 에 올린다.
    return (csv_bytes, 요약 dict)"""
    m = load_matches()
    if m.empty:
        return b"", dict(new=0, total_rows=0, total_games=0, years="-")
    m = m.drop_duplicates(subset=["match_datetime", "league", "home_team", "away_team", "bet_type", "line"], keep="last").sort_values(["match_datetime", "game_no"])
    m.insert(0, "season", m.match_datetime.str[:4])
    cols = ["season", "match_datetime", "sport", "league", "home_team", "away_team", "bet_type", "line", "odds1", "oddsx", "odds2",
            "open1", "openx", "open2", "open_line", "n_chg", "home_score", "away_score", "total_score", "round_label", "source_file", "uploaded_at", "row_key"]
    out = m[cols]
    path = os.path.join(DATA_DIR, "master", "master_games.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    prev_keys = set()
    prev = storage.fetch("master/master_games.csv") if not os.path.exists(path) else path
    if prev and os.path.exists(prev):
        try:
            prev_keys = set(pd.read_csv(prev, usecols=["row_key"]).row_key)
        except Exception:
            pass
    data = out.to_csv(index=False).encode("utf-8-sig")
    with open(path, "wb") as f:
        f.write(data)
    storage.push("master/master_games.csv")
    years = sorted(out.season.unique())
    games = out[["match_datetime", "league", "home_team", "away_team"]].drop_duplicates().shape[0]
    return data, dict(new=int((~out.row_key.isin(prev_keys)).sum()), total_rows=len(out), total_games=games,
                      years=f"{years[0]}~{years[-1]}" if len(years) > 1 else years[0])
