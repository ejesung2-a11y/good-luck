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
        cols = [r[1] for r in c.execute("PRAGMA table_info(matches)")]
        for col, typ in (("total_score", "REAL"), ("trend1", "INTEGER"), ("trendx", "INTEGER"), ("trend2", "INTEGER")):
            if col not in cols:
                c.execute(f"ALTER TABLE matches ADD COLUMN {col} {typ}")


def data_version():
    """데이터 버전(경기 행수, 마지막 저장시각, 파일 수). 파일 시각 대신 DB 를 직접 조회해 캐시가 낡은 채 남지 않게 한다."""
    init_sports_db()
    with _conn() as c:
        return c.execute("SELECT COUNT(*), COALESCE(MAX(uploaded_at), ''), (SELECT COUNT(*) FROM uploads) FROM matches").fetchone()


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


def clear_sports_db():
    with _conn() as c:
        c.execute("DELETE FROM matches"); c.execute("DELETE FROM uploads")
    _sync_db()


def load_matches():
    with _conn() as c:
        df = pd.read_sql("SELECT * FROM matches ORDER BY match_datetime, game_no", c)
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
    storage.push(_rel(p))
    return p


def save_matches(df, source_file, round_label, raw_bytes=None, saved_path=None, sync=True):
    """같은 경기(시각+종목+유형+라인+팀)가 이미 있으면 '최근 업로드'로 덮어쓴다."""
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
    with _conn() as c:
        c.execute("DELETE FROM matches WHERE source_file=?", (source_file,))
        for _, r in df.iterrows():
            key = "|".join(str(x) for x in [r["match_datetime"], r["league"], r["bet_type"],
                                            r["line"], r["home_team"], r["away_team"]])
            c.execute("""INSERT OR REPLACE INTO matches(row_key, source_file, round_label, game_no, match_datetime, league, sport,
                         bet_type, line, home_team, away_team, odds1, oddsx, odds2, home_score, away_score, uploaded_at, total_score,
                         trend1, trendx, trend2)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (key, source_file, round_label, str(r["game_no"]), r["match_datetime"], r["league"],
                       r["sport"], r["bet_type"], _nz(r["line"]), r["home_team"], r["away_team"],
                       _nz(r["odds1"]), _nz(r["oddsx"]), _nz(r["odds2"]),
                       _ni(r["home_score"]), _ni(r["away_score"]), ts,
                       _nz(r["total_score"]) if "total_score" in df.columns else None,
                       _ni(r["trend1"]) if "trend1" in df.columns else 0,
                       _ni(r["trendx"]) if "trendx" in df.columns else 0,
                       _ni(r["trend2"]) if "trend2" in df.columns else 0))
        c.execute("INSERT OR REPLACE INTO uploads VALUES(?,?,?,?,?,?)",
                  (source_file, round_label, fhash, ts, saved_path, len(df)))
    if saved_path:
        storage.push(_rel(saved_path))
    if sync:
        _sync_db()


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
def _ocr_pages(images, progress=None):
    import pytesseract
    from PIL import ImageOps
    texts = []
    for i, im in enumerate(images):
        im = im.convert("L")
        if im.width < 2400:                       # 작은 글씨 → 확대
            f = 2400 / im.width
            im = im.resize((int(im.width * f), int(im.height * f)))
        im = ImageOps.autocontrast(im)
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
SOCCER_HINT = r"INTL|네이션|월드컵|친선|유로|예선|챔피언스|유로파|컨퍼런스|리그|컵|EPL|라리가|분데스|세리에|리그1|MLS|남축|여축"
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

        # 유형
        bet, line = "일반", None
        mh = re.search(r"\bH\s*([+-]?\s*\d+(?:[.,]\d)?)", rest)
        mu = re.search(r"\b[UuO]\s*(\d+(?:[.,]\d)?)", rest)
        mw = re.search(r"승(\S)패", rest)
        if mw:
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
            three = (soccer3 and r["bet_type"] in ("일반", "H")) or is_margin_bet(r["bet_type"])
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


def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _score_matrix(lh, la, n=14):
    return [[_pois(i, lh) * _pois(j, la) for j in range(n)] for i in range(n)]


def _outcome_probs(M):
    ph = sum(M[i][j] for i in range(len(M)) for j in range(len(M)) if i > j)
    pd_ = sum(M[i][i] for i in range(len(M)))
    pa = sum(M[i][j] for i in range(len(M)) for j in range(len(M)) if i < j)
    return ph, pd_, pa


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
            notes.append(f"핸디-1 핸디무 {d*100:.0f}% = 홈이 1점차로 이길 확률 → 핸디시장 홈승 {(w+d)*100:.0f}%·무 {px_h*100:.0f}% (일반배당 홈승 {ph*100:.0f}%·무 {px*100:.0f}%)")
        elif line > 0 and k == 1:
            px_h = min(max(w - ph, 0.03), w - 0.01)
            ests.append((w - px_h, px_h, d + l, 0.3))
            notes.append(f"핸디+1 핸디무 {d*100:.0f}% = 원정이 1점차로 이길 확률 → 핸디시장 원정승 {(d+l)*100:.0f}%·무 {px_h*100:.0f}% (일반배당 원정승 {pa*100:.0f}%·무 {px*100:.0f}%)")
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
    """사이트가 표시한 배당 변동(↑상승/↓하락) 중 역배/인기 쪽 변동을 문장으로"""
    if base is None:
        return ""
    t1, t2 = base.get("trend1") or 0, base.get("trend2") or 0
    t1 = 0 if pd.isna(t1) else int(t1); t2 = 0 if pd.isna(t2) else int(t2)
    fav_home = ph >= pa
    t_fav, t_dog = (t1, t2) if fav_home else (t2, t1)
    out = []
    if t_dog < 0: out.append("역배 쪽 배당 하락↓")
    if t_dog > 0: out.append("역배 쪽 배당 상승↑")
    if t_fav > 0: out.append("인기 쪽 배당 상승↑")
    if t_fav < 0: out.append("인기 쪽 배당 하락↓")
    return ("배당변동: " + ", ".join(out)) if out else ""


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

        if bt == "일반" and model:
            ph, px, pa = model["ph"], model["px"], model["pa"]
            opts = [("홈승", ph), ("무", px), ("원정승", pa)] if model["three"] else [("홈승", ph), ("원정승", pa)]
            pick, pp = max(opts, key=lambda x: x[1])
            if model["three"] and px >= max(ph, pa) - 0.015 and px >= 0.27:      # 무승부가 거의 같은 수준이면 무를 예상으로
                pick, pp = "무", px
            ranked = sorted(opts, key=lambda x: -x[1])
            alt = next(f"{n} {p*100:.0f}%" for n, p in ranked if n != pick)
            score = _score_text(sport, model, pick)
            adj = model["adj"]
            adj_txt = f" / 학습보정 홈{adj[0]*100:+.1f}%p·원정{adj[1]*100:+.1f}%p(표본 {adj[2]}·{adj[3]}경기)" if (adj[2] or adj[3]) else " / 누적 학습표본 없음(시장확률 그대로)"
            adj_txt += f" / 해외축구 {model['cal_n']:,}경기 보정표 적용" if model.get("cal_n") else ""
            hn = f" / {model['hnote']}" if model.get("hnote") else ""
            why = (hn[3:] + " / " if hn else "") + (f"환수율 {payout:.1f}% 마진제거 → 홈 {ph*100:.1f}%"
                   + (f" / 무 {px*100:.1f}%" if model["three"] else "") + f" / 원정 {pa*100:.1f}%" + adj_txt)
            up = min(ph, pa)
            tn = _trend_note(base, ph, pa)
            res[idx] = dict(pick=pick, score=score, prob=pp, label=confidence_label(pp, 3 if model["three"] else 2),
                            why=why, upset=up, dist=opts, alt=alt,
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
            score = _score_text(sport, model, "홈승" if blend[0] >= blend[-1] else "원정승")
            gap = (model_p[0] - mk[0]) * 100 if model_p else 0
            why = (f"핸디 {line:+g} 배당 마진제거 {names[0]} {mk[0]*100:.1f}%"
                   + (f"/무 {mk[1]*100:.1f}%" if three_row else "") + f"/{names[-1]} {mk[-1]*100:.1f}%"
                   + (f" · 일반배당 기반 모델 커버확률 {model_p[0]*100:.1f}% (차이 {gap:+.1f}%p, 둘을 50:50 합성)" if model_p else ""))
            res[idx] = dict(pick=pick, score=score, prob=pp, label=confidence_label(pp, 3 if three_row else 2),
                            why=why, upset=min(blend[0], blend[-1]), dist=list(zip(names, blend)),
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
    return res


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
    score = "-"
    if model and sport == "농구" and "mu" in model:
        mu = model["mu"]
        m = max(mu, n + 1) if pick == "승" else (min(mu, -(n + 1)) if pick == "패" else max(-n, min(n, mu)))
        score = f"{round((model['total'] + m) / 2)} : {round((model['total'] - m) / 2)}"
    elif model and sport in ("축구", "야구") and "lam" in model:
        M = _score_matrix(*model["lam"], n=16)
        best, bp = (0, 0), -1
        for i in range(16):
            for j in range(16):
                d = i - j
                cat = "승" if d > n else ("패" if d < -n else str(n))
                if cat == pick and not (sport == "야구" and d == 0) and M[i][j] > bp:
                    best, bp = (i, j), M[i][j]
        score = f"{best[0]} : {best[1]}"
    why = (f"승{n}패 배당 마진제거 승 {mk[0]*100:.1f}% / {n} {mk[1]*100:.1f}% / 패 {mk[2]*100:.1f}%"
           + (f" · 일반배당 기반 점수분포 {mp[0]*100:.1f}/{mp[1]*100:.1f}/{mp[2]*100:.1f}% (50:50 합성)" if mp else " · 이 종목은 점수모델이 없어 시장확률만 사용"))
    return dict(pick=pick, score=score, prob=pp, label=confidence_label(pp, 3), why=why, upset=min(bl[0], bl[2]),
                dist=list(zip(names, bl)), alt=next(f"{n} {p*100:.0f}%" for n, p in sorted(zip(names, bl), key=lambda x: -x[1]) if n != pick))


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
                diff = i - j + line
                if diff > 1e-9: w += M[i][j]
                elif diff < -1e-9: l += M[i][j]
                else: d += M[i][j]
        return [w, d, l] if three else [w / (w + l), l / (w + l)]
    if sport == "농구" and "mu" in model:
        nd = NormalDist(model["mu"] + line, model["sig"])
        w = 1 - nd.cdf(0)
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
                             payout=1 / sum(1 / od[i] for i in avail), act_idx=names.index(act), fav_idx=fav_i,
                             fav_p=max(pr), t1=tr[0], tx=tr[1], t2=tr[2], match_datetime=r["match_datetime"]))
    return pd.DataFrame(rows)


def _side_col(i):
    return ("o1", "ox", "o2")[i]


def similar_stats(hist, r):
    """같은 종목·같은 유형(·같은 핸디라인)에서 '두 배당(홈·상대)이 모두 비슷했던' 과거 경기의 실제 결과 분포"""
    if hist is None or hist.empty or not has_odds(r):
        return 0, Counter()
    h = hist[(hist.sport == r["sport"]) & (hist.bet_type == r["bet_type"])]
    bt = r["bet_type"]
    if h.empty:
        return 0, Counter()
    if (bt == "H" or is_margin_bet(bt)) and pd.notna(r["line"]):
        h = h[(h.line - r["line"]).abs() < 1e-9]
    elif bt == "U" and pd.notna(r["line"]):
        h = h[(h.line - r["line"]).abs() <= max(0.5, 0.03 * r["line"])]
    o1, o2 = r["odds1"], r["odds2"]
    h = h[((h.o1 - o1).abs() <= max(0.06, 0.04 * o1)) & ((h.o2 - o2).abs() <= max(0.06, 0.04 * o2))]
    if pd.notna(r["oddsx"]):
        h = h[h.ox.notna() & ((h.ox - r["oddsx"]).abs() <= max(0.1, 0.05 * r["oddsx"]))]
    return len(h), Counter(h.actual)


def band_stats(hist, r, pick):
    """'내가 고른 쪽의 배당대'(상대 배당은 무관)에서의 실제 적중률 — 표본이 더 넓다"""
    if hist is None or hist.empty or not has_odds(r):
        return 0, None, None
    names = slot_names(r["bet_type"], r["line"])
    if pick not in names:
        return 0, None, None
    i = names.index(pick)
    od = (r["odds1"], r["oddsx"], r["odds2"])[i]
    if pd.isna(od):
        return 0, None, None
    h = hist[(hist.sport == r["sport"]) & (hist.bet_type == r["bet_type"])]
    if (r["bet_type"] == "H" or is_margin_bet(r["bet_type"])) and pd.notna(r["line"]):
        h = h[(h.line - r["line"]).abs() < 1e-9]
    h = h[(h[_side_col(i)] - od).abs() <= max(0.05, 0.08 * od)]
    if h.empty:
        return 0, None, od
    return len(h), float((h.actual == pick).mean()), od


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
    h["유형"] = h.bet_type.map(lambda b: {"일반": "일반", "H": "핸디", "U": "언더오버", "SUM": "홀짝"}.get(b, "승N패"))
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


def apply_history(res, hist):
    """예측 표에 사용자의 과거 데이터 통계를 붙이고 확률을 보정한다.
    ① 두 배당이 모두 비슷했던 경기의 결과(표본 5+ → 최대 ±5%p) ② 고른 쪽 배당대의 적중률(표본 8+ → 최대 ±3%p)
    ③ 일반 경기는 '역배(이변)' 과거 비율로 이변 확률도 보정."""
    cols = {k: [] for k in ("pred_hist", "pred_prob", "pred_label", "pred_why", "pred_dist", "pred_upset", "pred_upset_txt")}
    for _, r in res.iterrows():
        p, why, lines = r["pred_prob"], r["pred_why"], []
        dist, up, up_txt = r.get("pred_dist"), r.get("pred_upset"), r.get("pred_upset_txt")
        if pd.notna(p) and r["pred_pick"] not in ("검수 필요", "배당 미발매", "예측 제외"):
            pick = r["pred_pick"]
            adj = 0.0
            n, cnt = similar_stats(hist, r)
            if n >= 5:
                rate = cnt.get(pick, 0) / n
                a1 = max(-0.05, min(0.05, (rate - p) * n / (n + 40)))
                adj += a1
                top = " · ".join(f"{k} {v*100/n:.0f}%" for k, v in cnt.most_common(3))
                lines.append(f"📚 같은 배당 조합(홈·상대 모두 비슷) {n}경기: {top}")
                if r["bet_type"] == "일반" and pd.notna(up):
                    under = "원정승" if r["odds2"] > r["odds1"] else "홈승"
                    ur = cnt.get(under, 0) / n
                    up = up + max(-0.05, min(0.05, (ur - up) * n / (n + 40)))
                    lines.append(f"🎲 같은 배당 조합에서 역배({under}) 실제 {ur*100:.0f}% (배당 환산 {r['pred_upset']*100:.0f}%)")
            elif n > 0:
                lines.append(f"📚 같은 배당 조합 과거 {n}경기뿐(표본 부족, 참고만)")
            else:
                lines.append("📚 같은 배당 조합의 과거 기록 없음")
            nb, rb, od = band_stats(hist, r, pick)
            if nb >= 8:
                a2 = max(-0.03, min(0.03, (rb - p) * nb / (nb + 80)))
                adj += a2
                lines.append(f"📊 이 배당대({od:g}±8%)에서 '{pick}' 실제 적중 {rb*100:.0f}% ({nb}경기)")
            elif nb > 0:
                lines.append(f"📊 이 배당대 과거 {nb}경기뿐(표본 부족)")
            adj = max(-0.06, min(0.06, adj))
            if adj:
                why += f" · 과거데이터 보정 {adj*100:+.1f}%p"
                p = p + adj
                if isinstance(dist, list):
                    dist = _renorm_dist(dist, pick, adj)
            if r["bet_type"] == "일반" and pd.notna(up) and isinstance(dist, list):
                ph = dict(dist).get("홈승", 0.0); pa = dict(dist).get("원정승", 0.0)
                up_txt = upset_level(up, ph, pa)
        nway = 3 if (pd.notna(r["oddsx"]) or is_margin_bet(r["bet_type"])) else 2
        keep = r["pred_label"] in ("⚠️ 검수", "⏳ 대기", "⚪ 제외")
        cols["pred_hist"].append("\n".join(lines)); cols["pred_prob"].append(p); cols["pred_why"].append(why)
        cols["pred_label"].append(r["pred_label"] if keep or pd.isna(p) else confidence_label(p, nway))
        cols["pred_dist"].append(dist); cols["pred_upset"].append(up); cols["pred_upset_txt"].append(up_txt)
    res = res.copy()
    for k, v in cols.items():
        res[k] = v
    return res


def build_predictions(df_all, target_df, hist=None):
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
    for c in ("pred_dist", "pred_alt", "pred_draw", "pred_upset_txt", "pred_signal", "pred_upset"):
        if c not in res.columns:
            res[c] = None
    return apply_history(res, hist if hist is not None else history_table(learn))


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
            "U": ("언더", None, "오버"), "SUM": ("홀", None, "짝")}.get(bt, ("1", "X", "2"))


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
            grp = {"일반": "일반", "H": "핸디", "U": "언더오버"}.get(r["bet_type"], "승N패")
            out.append(dict(sport=sport, 유형=grp, 라벨=p["label"], 예상확률=p["prob"], 적중=p["pick"] == act))
    return pd.DataFrame(out), len(games)
