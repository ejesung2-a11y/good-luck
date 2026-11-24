import os
import re
import html
import streamlit as st
import pandas as pd
import sports
from datetime import timedelta

BADGE = {"축구": "#0284c7", "야구": "#16a34a", "농구": "#ea580c", "배구": "#9333ea"}
LABEL_BORDER = {"강력": "#34d399", "유력": "#facc15", "낮음": "#fb923c", "위험": "#f87171"}


def _ver():
    """데이터 버전(행수·마지막 저장시각·파일수). 데이터가 바뀔 때만 무거운 계산을 다시 한다."""
    return "|".join(str(x) for x in sports.data_version())


@st.cache_data(show_spinner=False, max_entries=3)
def _matches(ver):
    return sports.load_matches()


@st.cache_data(show_spinner=False, max_entries=3)
def _hist(ver):
    return sports.history_table(_matches(ver))


@st.cache_data(show_spinner=False, max_entries=3)
def _eval(ver):
    """앱이 기록해 둔 예측 vs 실제 결과"""
    return sports.eval_pred_log(_matches(ver))


@st.cache_data(show_spinner=False, max_entries=3)
def _selfcal(ver):
    return sports.selfcal_table(_eval(ver))


@st.cache_data(show_spinner="예측 계산 중…", max_entries=6)
def _predictions(ver, sport, bucket):
    """bucket = 10분 단위 시각. 같은 데이터·같은 10분 안에서는 다시 계산하지 않는다. 계산한 예측은 기록해 두었다가 결과와 비교한다."""
    m = _matches(ver)
    if m.empty:
        return pd.DataFrame()
    up = m[pd.to_datetime(m.match_datetime) > sports.now_kst()]
    if sport != "전체":
        up = up[up.sport == sport]
    if up.empty:
        return pd.DataFrame()
    pr = sports.build_predictions(m, up, hist=_hist(ver), selfcal=_selfcal(ver))
    sports.log_predictions(pr)
    return pr


@st.cache_data(show_spinner=False, max_entries=3)
def _learn_tables(ver):
    h = _hist(ver)
    return dict(odds=sports.odds_winrate_table(h), hand=sports.handicap_winrate_table(h),
                payout=sports.payout_table(h), trend=sports.trend_table(h),
                move=sports.movement_table(h), close=sports.closegame_table(_matches(ver)))


def _post(df, method):
    """OCR 로 읽은 팀명은 이미 저장된(정확히 읽힌) 팀명 중 가장 비슷한 것으로 맞춘다"""
    if method.startswith("OCR") and not df.empty:
        df, _ = sports.snap_team_names(df, sports.known_teams())
    return df


def sidebar():
    st.sidebar.markdown("### 📄 배당 캡처 업로드")
    ver = st.session_state.get("sp_up_ver", 0)           # 처리 후 이 번호를 올리면 업로드 칸이 새로 만들어져 비워진다
    files = st.sidebar.file_uploader("PDF / 이미지(PNG·JPG) — 여러 개 가능", type=["pdf", "png", "jpg", "jpeg"],
                                     accept_multiple_files=True, key=f"sp_files_{ver}")
    pasted = st.sidebar.text_area("또는 와이즈토토 표를 복사해서 붙여넣기 (가장 정확)", height=90, key=f"sp_paste_{ver}")
    round_in = st.sidebar.text_input("회차 이름 (비우면 파일명에서 자동 · 예: 137회차)", value="")
    if st.sidebar.button("🔍 읽기(파싱) 시작", use_container_width=True):
        st.session_state["pending"] = {}
        bar = st.sidebar.progress(0.0)
        for f in files or []:
            raw = f.getvalue()
            text, method = sports.file_to_text(f.name, raw, progress=bar.progress)
            st.session_state["pending"][f.name] = dict(
                df=_post(sports.parse_text(text, year=sports.year_from_name(f.name)), method), raw=raw, path=sports.stash_raw(f.name, raw), method=method,
                round=round_in or sports.make_round_label(f.name, text), header=sports.header_round(text))
        if pasted.strip():
            name = f"붙여넣기_{sports.now_kst():%Y%m%d_%H%M%S}.txt"
            st.session_state["pending"][name] = dict(
                df=sports.parse_text(pasted), raw=pasted.encode(), path=sports.stash_raw(name, pasted.encode()), method="텍스트 붙여넣기",
                round=round_in or sports.guess_round_label(pasted), header=sports.header_round(pasted))
        bar.empty()
        if files or pasted.strip():
            st.session_state["sp_up_ver"] = ver + 1      # 읽기가 끝났으니 업로드 목록·붙여넣기 칸 비우기
            st.rerun()
    st.sidebar.markdown("---")
    return st.sidebar.selectbox("종목", ["전체", "축구", "야구", "농구", "배구"])


def pending_review():
    pend = st.session_state.get("pending") or {}
    if st.session_state.get("save_msg"):
        st.success(st.session_state.pop("save_msg"))
    if not pend:
        return
    top = st.container()
    edited, labels, confirm = {}, {}, {}
    known = sports.known_teams()
    for name, p in list(pend.items()):
        df = p["df"]
        with st.expander(f"📝 {name}  ({p['method']}, {len(df)}행)", expanded=len(pend) == 1):
            if df.empty:
                st.error("읽힌 경기가 없습니다. 텍스트 붙여넣기를 사용하거나 해상도가 높은 캡처를 올려 주세요.")
                continue
            bad = (~df.apply(lambda r: sports.odds_ok(r), axis=1) & df.odds1.notna() & (df.bet_type != "SUM")).sum()
            if p["method"].startswith("OCR"):
                st.warning(f"OCR 은 숫자를 틀리게 읽을 수 있습니다. 배당이 비정상인 {bad}행은 예측에서 자동 제외됩니다. 표에서 직접 고친 뒤 저장하세요.")
            dts = pd.to_datetime(df.match_datetime, errors="coerce").dropna()
            if len(dts):
                span = (dts.max() - dts.min()).days
                ym = re.search(r"(\d{4})년", p["round"] or "")
                bad_year = bool(ym) and int(ym.group(1)) != dts.min().year and not (dts.min().month >= 11 and dts.max().year == int(ym.group(1)) + 1)
                st.caption(f"📅 경기 날짜 범위: {dts.min():%Y-%m-%d} ~ {dts.max():%Y-%m-%d} ({span}일)")
                if span > 14 or bad_year:
                    st.warning("⚠️ 경기 날짜 범위가 이상합니다(14일 초과 또는 회차 연도와 다름). 연도·월이 잘못 읽혔을 수 있으니 표의 match_datetime 을 확인하세요.")
            hy, hn = p.get("header", (None, None))
            m_ = re.search(r"(\d+)회차", p["round"] or "")
            if hn and m_ and int(m_.group(1)) != hn:
                st.warning(f"⚠️ 파일명/입력 회차({p['round']})와 PDF 본문 머리말({hy}년 {hn}회차)이 다릅니다. 회차 칸을 확인해 주세요.")
            labels[name] = st.text_input("회차 (파일명에서 자동 · 같은 회차가 여러 개면 저장 시 -1, -2 로 구분)", value=p["round"], key=f"rl_{name}")
            flags = sports.review_flags(df, known)
            view = df.copy()
            view.insert(0, "확인필요", flags)
            n_flag = sum(1 for f_ in flags if f_)
            st.caption(f"🔎 [확인필요] {n_flag}행 — 표 맨 왼쪽 칸에 이유가 표시됩니다. ('새팀명?'은 이전에 저장된 적 없는 팀이라는 뜻이며 오류가 아닐 수도 있습니다.) 승인(저장)하기 전에는 통계·학습에 반영되지 않습니다.")
            edited[name] = st.data_editor(view, num_rows="dynamic", use_container_width=True, key=f"ed_{name}", height=300,
                                          column_config={"확인필요": st.column_config.TextColumn("확인필요", disabled=True)})
            if p["method"].startswith("OCR"):
                confirm[name] = st.checkbox("OCR 로 읽은 값(배당·팀명·기준값)을 눈으로 확인하고 고쳤습니다", key=f"cf_{name}")
    with top:
        st.markdown(f"**읽은 파일 {len(pend)}개 · 합계 {sum(len(p['df']) for p in pend.values())}행** — 검수가 끝났으면 한 번에 저장하세요.")
        if confirm and any(not v for v in confirm.values()):
            st.warning("OCR 로 읽은 파일은 각 파일의 '눈으로 확인했습니다' 체크 후에만 저장할 수 있습니다. (이미지 캡처 OCR 은 배당이 자주 틀립니다. 모바일에서는 '인쇄 → PDF 저장'을 쓰면 정확합니다.)")
        st.caption("📌 저장 규칙: 아직 시작 안 한 경기는 **가장 최근 회차의 최신 업로드**로만 덮어씁니다. 결과가 확정됐거나 이미 시작한 경기, 더 최신 회차에 이미 있는 경기는 덮어쓰지 않고 참고·학습용으로 보존합니다(이미 시작한 경기에 결과가 담긴 파일이 오면 결과만 채웁니다).")
        c1, c2 = st.columns(2)
        if c1.button(f"💾 {len(edited)}개 파일 일괄 저장 (같은 경기는 최신으로 덮어쓰기)", type="primary", use_container_width=True,
                     disabled=(not edited) or any(not v for v in confirm.values())):
            final = sports.resolve_round_labels({n: (labels[n] or "미지정") for n in edited})
            tot = {}
            for n, df_ in edited.items():
                st_ = sports.save_matches(df_.drop(columns=["확인필요"], errors="ignore"), n, final[n], saved_path=pend[n].get("path"), sync=False)
                for k_, v_ in st_.items():
                    tot[k_] = tot.get(k_, 0) + v_
            sports._sync_db()                      # R2 로는 마지막에 한 번만 올린다(파일 수만큼 반복하면 느림)
            st.cache_data.clear()
            st.session_state["pending"] = {}
            kept = tot.get("보존(결과 확정)", 0) + tot.get("보존(이미 시작)", 0) + tot.get("보존(더 최신 회차)", 0)
            st.session_state["save_msg"] = (f"{len(edited)}개 파일 저장 완료 → " + ", ".join(f"{n} = {final[n]}" for n in edited)
                                            + f" | 신규 {tot.get('신규', 0)}줄 · 최신으로 갱신 {tot.get('갱신', 0)}줄 · 덮어쓰지 않고 보존 {kept}줄"
                                            + (f" (결과 확정 {tot.get('보존(결과 확정)', 0)} · 이미 시작 {tot.get('보존(이미 시작)', 0)} · 더 최신 회차 {tot.get('보존(더 최신 회차)', 0)})" if kept else ""))
            st.rerun()
        if c2.button("전체 버리기", use_container_width=True):
            for n, p in pend.items():
                if p.get("path") and os.path.exists(p["path"]):
                    os.remove(p["path"]); sports.storage.remove(sports._rel(p["path"]))
            st.session_state["pending"] = {}
            st.rerun()


def _ok(v):
    """None / NaN / 빈 문자열이면 False (화면에 'nan' 이 찍히지 않게)"""
    return isinstance(v, str) and v.strip() != "" and v.strip().lower() != "nan"


BAR_COLORS = ["#38bdf8", "#94a3b8", "#f87171"]


def _bar(dist):
    """승/무/패 확률 막대 (홈=파랑, 무=회색, 원정=빨강)"""
    if not isinstance(dist, list) or not dist:
        return ""
    cols = [BAR_COLORS[0], BAR_COLORS[2]] if len(dist) == 2 else BAR_COLORS
    segs = "".join(
        f"<div style='width:{max(p, 0.001)*100:.1f}%;background:{c};color:#0f172a;font-size:10px;font-weight:bold;text-align:center;overflow:hidden;white-space:nowrap'>{html.escape(str(n))} {p*100:.0f}%</div>"
        for (n, p), c in zip(dist, cols))
    return f"<div style='display:flex;height:18px;border-radius:5px;overflow:hidden;margin:4px 0'>{segs}</div>"


def _odds_txt(r):
    """현재 배당. 내가 올린 파일들에서 배당이 바뀐 적이 있으면 '처음값→현재값' 으로, 핸디·언더오버 기준점이 바뀌었으면 함께 표시"""
    out = []
    changed = r.get("n_chg") and int(r.get("n_chg") or 0) > 0 and pd.isna(r.get("open_line"))
    for o, t, op in ((r["odds1"], r.get("trend1"), r.get("open1")), (r["oddsx"], r.get("trendx"), r.get("openx")), (r["odds2"], r.get("trend2"), r.get("open2"))):
        if pd.isna(o):
            continue
        if changed and pd.notna(op) and abs(o - op) >= 0.005:
            col = "#38bdf8" if o < op else "#f87171"
            out.append(f"<span style='color:#64748b;text-decoration:line-through'>{op:g}</span><span style='color:{col}'>→{o:g}{'↓' if o < op else '↑'}</span>")
            continue
        arrow = "" if t is None or pd.isna(t) or int(t) == 0 else ("<span style='color:#38bdf8'>↓</span>" if int(t) < 0 else "<span style='color:#f87171'>↑</span>")
        out.append(f"{o:g}{arrow}")
    txt = " / ".join(out) or "미발매"
    if pd.notna(r.get("open_line")) and pd.notna(r.get("line")):
        txt += f" <span style='color:#facc15'>🔔기준점 {r['open_line']:g}→{r['line']:g}</span>"
    elif changed:
        txt += f" <span style='color:#facc15'>🔔변경 {int(r['n_chg'])}회</span>"
    return txt


def _card(g, notes=None):
    head = g.iloc[0]
    memo = (notes or {}).get(sports.game_key(head))
    color = BADGE.get(head["sport"], "#475569")
    parts = [f"""<span style="background:{color};color:#fff;padding:2px 6px;border-radius:4px;font-size:11px">{head['sport']} · {html.escape(str(head['league']))}</span>
      <b style="color:#38bdf8;margin-left:6px">{head['match_datetime'][:16]}</b>
      <span style="font-size:10px;color:#64748b;margin-left:6px">📄 {html.escape(str(head.get('round_label', '')))}</span>
      <div style="font-size:16px;font-weight:bold;margin:6px 0">🏠 {html.escape(str(head['home_team']))} <span style="color:#64748b;font-size:12px">vs</span> ✈️ {html.escape(str(head['away_team']))}</div>"""
             + (f"<div style='font-size:12px;color:#fde68a'>📝 {html.escape(memo)}</div>" if memo else "")]
    for _, r in g.iterrows():
        bt = r["bet_type"]
        tname = _tname(bt, r["line"])
        prob = f"{r['pred_prob']*100:.0f}%" if pd.notna(r["pred_prob"]) else "-"
        row = [f"<div style='border-top:1px solid #334155;padding-top:6px;margin-top:6px'><b>{tname}</b> · 배당 {_odds_txt(r)} → <b style='color:#facc15'>{html.escape(str(r['pred_pick']))}</b> ({prob}) {r['pred_label']} · 스코어 {html.escape(str(r['pred_score']))}</div>"]
        row.append(_bar(r.get("pred_dist")))
        chips = []
        if _ok(r.get("pred_alt")): chips.append(f"대안 {r['pred_alt']}")
        for k_ in ("pred_draw", "pred_upset_txt", "pred_signal"):
            if _ok(r.get(k_)): chips.append(r[k_])
        if chips:
            row.append(f"<div style='font-size:12px;color:#e2e8f0'>{' · '.join(html.escape(str(c)) for c in chips)}</div>")
        row.append(f"<div style='font-size:11px;color:#94a3b8;margin-top:2px'>🔍 {html.escape(str(r['pred_why']))}</div>")
        if bt in ("H", "hH") and pd.notna(r["line"]) and r["sport"] == "축구" and abs(r["line"] - round(r["line"])) < 1e-9 and abs(r["line"]) >= 1:
            row.append(f"<div style='font-size:11px;color:#fde68a'>ℹ️ {html.escape(sports.handicap_legend(r['line']))}</div>")
        if _ok(r.get("pred_hist")):
            row.append(f"<div style='font-size:11px;color:#a5b4fc'>{html.escape(str(r['pred_hist'])).replace(chr(10), '<br>')}</div>")
        parts.append("".join(row))
    st.markdown(f"<div style='background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px;margin-bottom:10px'>{''.join(parts)}</div>", unsafe_allow_html=True)


def _paginate(groups, key, default=20):
    """카드가 수백 개일 때 한 화면에 N경기씩 나눠 보기(스크롤이 길어지는 문제 완화)"""
    n = len(groups)
    if n <= 10:
        return groups
    c1, c2 = st.columns(2)
    per = c1.select_slider("한 번에 보기(경기 수)", [10, 20, 40, 80, 200], value=default, key=f"pp_{key}")
    pages = max(1, -(-n // per))
    opts = list(range(1, pages + 1))
    page = c2.selectbox("페이지", opts, format_func=lambda i: f"{i} / {pages}  ({(i-1)*per+1}~{min(i*per, n)}경기)", key=f"pg_{key}_{per}")
    return groups[(page - 1) * per: page * per]


TYPE_ORDER = {"일반": 0, "승": 1, "H": 2, "U": 3, "SUM": 4, "h": 5, "hH": 6, "hU": 7}


def _tname(bt, line):
    """유형 표시 이름"""
    ln = pd.notna(line)
    return {"일반": "일반", "H": f"핸디 {line:+g}" if ln else "핸디", "U": f"언더오버 {line:g}" if ln else "언더오버", "SUM": "홀짝",
            "h": "⏱️ 전반 일반", "hH": f"⏱️ 전반 핸디 {line:+g}" if ln else "⏱️ 전반 핸디",
            "hU": f"⏱️ 전반 언더오버 {line:g}" if ln else "⏱️ 전반 언더오버"}.get(bt, bt)


def _fmt(v):
    return "-" if v is None or pd.isna(v) else f"{v:g}"


def _past_card(g):
    """한 경기(같은 시각·리그·홈·원정)를 카드 하나로: 유형별 줄, 결과, 이긴 배당은 초록색"""
    rows = sorted(g.to_dict("records"), key=lambda r: (TYPE_ORDER.get("승" if sports.is_margin_bet(r["bet_type"]) else r["bet_type"], 9), str(r["game_no"])))
    head = rows[0]
    base = next((r for r in rows if r["bet_type"] == "일반"), None)
    hs = as_ = None
    if base is not None and pd.notna(base["home_score"]):
        hs, as_ = int(base["home_score"]), int(base["away_score"])
    color = BADGE.get(head["sport"], "#475569")
    score_html = (f"<span style='background:#facc15;color:#111;padding:2px 8px;border-radius:6px;font-weight:bold;margin-left:8px'>최종 {hs} : {as_}</span>"
                  if hs is not None else "<span style='color:#94a3b8;margin-left:8px;font-size:12px'>결과 없음(경기전·진행중)</span>")
    trs = ""
    for r in rows:
        bt = r["bet_type"]
        names = sports.slot_names(bt, r["line"])
        act = sports._actual(r, hs, as_) if hs is not None else None
        tname = _tname(bt, r["line"])
        cells = ""
        for nm, odd in zip(names, (r["odds1"], r["oddsx"], r["odds2"])):
            if nm is None:
                cells += "<td style='padding:4px 6px'></td>"
                continue
            if act is None:
                st_ = "background:#1e293b;color:#e2e8f0"
            elif nm == act:
                st_ = "background:#15803d;color:#fff;font-weight:bold;box-shadow:inset 0 0 0 2px #4ade80"
            else:
                st_ = "background:#0f172a;color:#64748b"
            cells += f"<td style='padding:4px 6px;text-align:center;border-radius:6px;{st_}'><div style='font-size:10px;opacity:.85'>{nm}</div><div style='font-size:14px'>{_fmt(odd)}</div></td>"
        res = f"<b style='color:#facc15'>{act}</b>" if act else "<span style='color:#475569'>-</span>"
        trs += (f"<tr><td style='padding:4px 6px;color:#cbd5e1;white-space:nowrap;font-size:12px'>{tname}</td>{cells}"
                f"<td style='padding:4px 6px;text-align:center;white-space:nowrap'>{res}</td></tr>")
    st.markdown(f"""<div style="background:#1e293b;border:1px solid #334155;border-radius:12px;padding:10px 12px;margin-bottom:10px">
      <span style="background:{color};color:#fff;padding:2px 6px;border-radius:4px;font-size:11px">{head['sport']} · {head['league']}</span>
      <b style="color:#38bdf8;margin-left:6px">{head['match_datetime'][:16]}</b>{score_html}
      <div style="font-size:10px;color:#64748b;margin-top:2px">📄 {html.escape(str(head.get('source_file', '')))} · {html.escape(str(head.get('round_label', '')))}</div>
      <div style="font-size:16px;font-weight:bold;margin:6px 0">🏠 {head['home_team']} <span style="color:#64748b;font-size:12px">vs</span> ✈️ {head['away_team']}</div>
      <div style="overflow-x:auto"><table style="border-collapse:separate;border-spacing:4px 3px;width:100%"><tr style="color:#64748b;font-size:10px"><td>유형</td><td colspan="3" style="text-align:center">배당 (초록 = 실제 결과)</td><td style="text-align:center">결과</td></tr>{trs}</table></div></div>""",
                unsafe_allow_html=True)


def render(selected_sport):
    sports.init_sports_db()
    now = sports.now_kst()
    st.markdown("<h2 style='text-align:center'>⚽ 스포츠 토토 배당 분석 Pro</h2>", unsafe_allow_html=True)
    st.markdown(f"<p style='text-align:center;color:#38bdf8;font-size:13px'>⏱️ 한국시간 기준 {now:%Y-%m-%d %H:%M}</p>",
                unsafe_allow_html=True)
    if st.button("🔄 새로고침 (현재 시각 기준 경기전 다시 불러오기)", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    pending_review()
    if sports.storage.enabled() and sports.storage._state["error"]:
        st.error("☁️ R2 저장 오류: " + sports.storage._state["error"])

    ver = _ver()
    df = _matches(ver)
    n_db = sports.data_version()[0]
    with st.expander(f"🩺 데이터 진단 — DB {n_db}행 · 화면 {len(df)}행", expanded=(len(df) != n_db or df.empty)):
        if len(df) != n_db:
            st.error("DB 와 화면의 행수가 다릅니다(계산 결과가 낡았습니다). 아래 버튼으로 다시 계산하세요.")
        if df.empty:
            st.write("저장된 경기가 없습니다. 사이드바에서 PDF 를 올리고 '읽기' → '일괄 저장'을 눌러야 DB 에 들어갑니다.")
            if sports.storage.enabled():
                n_orig = sum(1 for rel, _ in sports.storage.list_keys() if re.fullmatch(r"uploads/\d{8}_\d{6}_[^/]+\.(pdf|png|jpe?g|txt)", rel, re.I))
                if n_orig:
                    st.info(f"☁️ R2 에 업로드 원본 {n_orig}개가 남아 있습니다. DB 만 비어 있다면 원본을 다시 읽어 복구할 수 있습니다(다시 올릴 필요 없음).")
                    if st.button("♻️ R2 원본으로 DB 복구", type="primary", key="rebuild_sp"):
                        bar = st.progress(0.0)
                        ok, skipped, fail = sports.rebuild_from_storage(bar.progress)
                        st.cache_data.clear()
                        st.session_state["save_msg"] = f"복구 완료: {ok}개 파일 읽음" + (f" · 실패 {len(fail)}: {fail[:3]}" if fail else "")
                        st.rerun()
        else:
            dts_ = pd.to_datetime(df.match_datetime, errors="coerce")
            fut = df[dts_ > now]
            fin = df[df.home_score.notna() & (df.bet_type == "일반")]
            st.write(f"- 현재 한국시간: **{now:%Y-%m-%d %H:%M}**")
            st.write(f"- 저장된 파일 {df.source_file.nunique()}개 · 경기 날짜 {dts_.min():%Y-%m-%d} ~ {dts_.max():%Y-%m-%d}")
            st.write(f"- **아직 시작 안 한 경기(경기전 예측 대상): {fut[['match_datetime','home_team','away_team']].drop_duplicates().shape[0]}경기**")
            st.write(f"- **결과(최종 스코어)가 있는 경기(학습 대상): {len(fin)}경기**")
            if fut.empty:
                st.warning("저장된 경기가 전부 현재 시각 이전입니다. 경기전 예측과 조합 화면은 비어 있는 것이 정상입니다. "
                           "지난 경기는 📜 지난 경기, 통계는 📈 학습 현황에서 보시고, 경기전 예측을 원하면 아직 시작 안 한 경기가 있는 최신 회차 PDF 를 올려 주세요.")
            if fin.empty:
                st.warning("스코어가 있는 끝난 경기가 없어 학습 통계가 비어 있습니다. 지난 회차 PDF(결과가 표시된 것)를 올려야 학습됩니다.")
        if st.button("🧹 캐시 비우고 다시 계산", key="diag_clear"):
            st.cache_data.clear()
            st.rerun()
    SECTIONS = ["🎯 경기전 예측", "🧩 조합·이변", "📜 지난 경기", "📂 파일 관리", "💾 백업/복구", "📈 학습 현황"]
    # 탭(st.tabs)은 모든 탭을 매번 계산하지만, 선택 메뉴는 고른 화면만 계산해서 훨씬 빠르다
    section = st.radio("메뉴", SECTIONS, horizontal=True, label_visibility="collapsed", key="sp_section")
    bucket = int(now.timestamp() // 600)

    def pr_all_fn():
        # 핸디·언더오버 행까지 함께 있어야 교차확인(핸디무→일반승/무)이 되므로, 전체 행으로 예측(캐시)한 뒤 화면에서 걸러낸다
        return _predictions(ver, selected_sport, bucket) if not df.empty else pd.DataFrame()

    if section == SECTIONS[0]:
        pr_all = pr_all_fn()
        notes = sports.load_notes()
        if df.empty:
            st.info("저장된 경기가 없습니다. 좌측에서 업로드하세요.")
        else:
            days = sorted({d.date() for d in pd.to_datetime(pr_all.match_datetime)}) if not pr_all.empty else []
            pick_day = st.selectbox("날짜", ["오늘 이후 전부"] + [d.strftime("%Y-%m-%d") for d in days])
            bt = st.radio("유형", ["전체", "일반", "핸디(H)", "언더오버(U)"], horizontal=True)
            pr = pr_all
            if not pr.empty:
                if pick_day != "오늘 이후 전부":
                    pr = pr[pr.match_datetime.str.startswith(pick_day)]
                pr = pr[{"전체": pr.bet_type.notna(), "일반": pr.bet_type == "일반", "핸디(H)": pr.bet_type == "H",
                         "언더오버(U)": pr.bet_type == "U"}[bt]]
            st.caption(f"경기전 {pr[['match_datetime','home_team','away_team']].drop_duplicates().shape[0] if not pr.empty else 0}경기 · 누적 DB {len(df)}행 · 🔵홈승 ⚪무 🔴원정승 · 배당 옆 ↓↑는 사이트 표시 변동")
            if pr.empty:
                st.info("표시할 경기전 경기가 없습니다. 위의 '🩺 데이터 진단'에서 이유를 확인하세요(저장된 경기가 모두 지난 경기이거나, 선택한 날짜·종목·유형에 해당이 없을 수 있습니다).")
            else:
                groups = [g for _, g in pr.groupby(["match_datetime", "league", "home_team", "away_team"], sort=False)]
                for g in _paginate(groups, f"fut_{pick_day}_{bt}"):
                    _card(g, notes)

    if section == SECTIONS[1]:
        pr_all = pr_all_fn()
        c_r1, c_r2 = st.columns([3, 1])
        c_r1.markdown("#### 🧩 조합 · 이변")
        if c_r2.button("🔄 새 게임 예측", use_container_width=True, key="refresh_combo"):
            st.cache_data.clear()
            st.rerun()
        if pr_all.empty:
            st.info("경기전 데이터가 없습니다. 🩺 데이터 진단을 확인하세요.")
        else:
            # ── 배당이 바뀐 경기 ──
            chg = pr_all[(pr_all.n_chg > 0) & pr_all.pred_prob.notna() & (pr_all.bet_type.isin(["일반", "H", "U", "승N패"]) | pr_all.bet_type.map(sports.is_margin_bet))]
            with st.expander(f"🔔 배당·기준점이 바뀐 경기 {chg[['match_datetime','home_team','away_team']].drop_duplicates().shape[0]}경기 (같은 경기를 다시 올렸을 때의 변화)", expanded=False):
                if chg.empty:
                    st.caption("아직 같은 경기를 두 번 이상 올려서 변화가 잡힌 경기가 없습니다.")
                else:
                    rows_ = []
                    for r in chg.to_dict("records"):
                        names = sports.slot_names(r["bet_type"], r["line"])
                        parts = [f"{n} {op:g}→{cu:g}" for n, op, cu in zip(names, (r["open1"], r["openx"], r["open2"]), (r["odds1"], r["oddsx"], r["odds2"]))
                                 if n and pd.notna(op) and pd.notna(cu) and abs(op - cu) >= 0.005]
                        if pd.notna(r["open_line"]):
                            parts.insert(0, f"기준점 {r['open_line']:g}→{r['line']:g}")
                        if parts:
                            rows_.append({"시각": r["match_datetime"], "경기": f"{r['home_team']} vs {r['away_team']}", "유형": _tname(r["bet_type"], r["line"]),
                                          "변화(처음→현재)": " · ".join(parts), "횟수": r["n_chg"]})
                    st.dataframe(pd.DataFrame(rows_), hide_index=True, use_container_width=True)
            # ── 이변 후보 ──
            st.markdown("##### 🎲 이변(역배) 후보 · 일반 경기")
            ge = pr_all[(pr_all.bet_type == "일반") & pr_all.pred_upset.notna() & pr_all.pred_dist.notna()].copy()
            if not ge.empty:
                ge["역배확률"] = (ge.pred_upset * 100).round(0).astype(int)
                ge["무확률"] = ge.pred_dist.map(lambda d: round(dict(d).get("무", 0) * 100) if isinstance(d, list) and "무" in dict(d) else None)
                ge["경기"] = ge.home_team + " vs " + ge.away_team
                ge["예상"] = ge.pred_pick + " (" + (ge.pred_prob * 100).round(0).astype(int).astype(str) + "%)"
                top = ge.sort_values("역배확률", ascending=False).head(10)[["match_datetime", "league", "경기", "예상", "역배확률", "무확률", "pred_upset_txt", "pred_signal"]]
                top.columns = ["시각", "리그", "경기", "예상", "역배%", "무%", "판정", "배당변동"]
                st.dataframe(top, hide_index=True, use_container_width=True)
            # ── 조합 찾기 ──
            st.markdown("##### 🔎 조합 찾기")
            f1, f2, f3 = st.columns(3)
            n_legs = f1.slider("경기 수 (1 = 단통)", 1, 8, 3, key="cb_n")
            min_p = f2.slider("유력 기준: 최소 확률 %", 30, 90, 55, 5, key="cb_p") / 100
            dog_sel = f3.selectbox("역배 경기 수", ["상관없음", "0 (역배 없음)", "1", "2", "3"], key="cb_dog")
            dog_legs = None if dog_sel == "상관없음" else int(dog_sel.split()[0])
            g1, g2 = st.columns(2)
            dog_p = g1.slider("역배 경기의 최소 확률 %", 15, 50, 25, 5, key="cb_dp") / 100 if dog_legs != 0 else 0.25
            odds_rng = g2.slider("합산 배당 범위 (배)", 1.0, 300.0, (1.5, 30.0), 0.5, key="cb_odds")
            h1, h2, h3 = st.columns(3)
            types_sel = h1.multiselect("베팅 유형", ["일반", "H", "U", "승N패"], default=["일반", "H", "U"],
                                       format_func=lambda x: {"일반": "일반(승무패)", "H": "핸디", "U": "언더오버", "승N패": "승N패"}[x], key="cb_types")
            sp_all = sorted(pr_all.sport.dropna().unique().tolist())
            sp_sel = h2.multiselect("종목", sp_all, default=sp_all, key="cb_sp")
            days = sorted({str(x)[:10] for x in pr_all.match_datetime})
            day_sel = h3.selectbox("날짜", ["전체"] + days, key="cb_day")
            rank = st.radio("정렬 기준", ["기대환급", "적중확률", "합산배당"], horizontal=True, key="cb_rank")
            res_, tested = sports.find_combos(pr_all, n_legs=n_legs, min_p=min_p, dog_legs=dog_legs, dog_min_p=dog_p,
                                              total_odds=odds_rng, types=types_sel, sports=sp_sel,
                                              day=None if day_sel == "전체" else day_sel, rank=rank)
            if not res_:
                st.info("조건에 맞는 조합이 없습니다. 최소 확률을 낮추거나, 합산 배당 범위·유형·날짜를 넓혀 보세요.")
            else:
                st.caption(f"조건에 맞는 조합 상위 {len(res_)}개 (검토한 후보 조합 {tested:,}개)")
                for i, c in enumerate(res_, 1):
                    with st.expander(f"{i}위 · 적중확률 {c['joint_p']*100:.1f}% · 합산배당 {c['joint_odds']:.2f}배 · 기대환급 {c['ev']*100:.0f}% · 역배 {c['n_dog']}개", expanded=i == 1):
                        for l in c["legs"]:
                            dog = "🎲역배 " if l["_dog"] else ""
                            st.markdown(f"- {dog}**{l['home_team']} vs {l['away_team']}** · {_tname(l['bet_type'], l['line'])} → **{l['pred_pick']}** (배당 {l['_odds']:g}, 확률 {l['pred_prob']*100:.0f}%) {l['pred_label']} · {l['match_datetime'][5:16]}")
            st.warning("기대환급이 100%를 넘는 조합은 드뭅니다. 확률은 배당과 과거 기록에서 나온 값이라 시장보다 크게 정확하다고 보장할 수 없고, 선수·감독 상태와 경기 환경은 반영하지 못합니다. 1경기(단통)는 '가장 믿는 한 경기', 2경기 안전형은 '보험(복식)'으로 쓸 수 있습니다.")
            # ── 경기 메모 ──
            with st.expander("📝 경기 메모 (부상·선발·연전 등 내가 아는 변수를 적어 두기)"):
                games_ = pr_all[["match_datetime", "league", "home_team", "away_team"]].drop_duplicates()
                opts = {f"{r.match_datetime[5:16]} {r.home_team} vs {r.away_team}": sports.game_key(r._asdict()) for r in games_.itertuples()}
                pick_g = st.selectbox("경기 선택", list(opts), key="memo_g")
                cur_note = sports.load_notes().get(opts[pick_g], "")
                memo_txt = st.text_input("메모", value=cur_note, key=f"memo_t_{opts[pick_g]}")
                if st.button("💾 메모 저장", key="memo_save"):
                    sports.save_note(opts[pick_g], memo_txt)
                    st.success("저장했습니다. 메모는 경기전 카드에 표시됩니다(예측 확률 자체는 자동으로 바꾸지 않습니다).")

    if section == SECTIONS[2]:
        if df.empty:
            st.info("데이터 없음")
        else:
            mode = st.radio("조회 기준", ["날짜", "회차"], horizontal=True)
            if mode == "날짜":
                ds = sorted({x[:10] for x in df.match_datetime}, reverse=True)
                key = st.selectbox("날짜 선택", ds)
                sub = df[df.match_datetime.str.startswith(key)]
            else:
                key = st.selectbox("회차 선택", sorted(df.round_label.dropna().unique(), key=sports.round_sort_key, reverse=True))
                sub = df[df.round_label == key]
            if selected_sport != "전체":
                sub = sub[sub.sport == selected_sport]
            keys = ["match_datetime", "league", "home_team", "away_team"]
            ngames = sub[keys].drop_duplicates().shape[0]
            st.caption(f"{ngames}경기 · 카드 안의 초록색 배당이 실제 결과입니다(최종 스코어가 있는 경기만).")
            groups = [g for _, g in sub.sort_values(["match_datetime", "game_no"]).groupby(keys, sort=False)]
            for g in _paginate(groups, f"past_{mode}_{key}"):
                _past_card(g)
            with st.expander("📋 원본 표로 보기"):
                st.dataframe(sub[["game_no", "match_datetime", "league", "bet_type", "line", "home_team", "away_team",
                                  "odds1", "oddsx", "odds2", "home_score", "away_score"]],
                             use_container_width=True, hide_index=True)

    if section == SECTIONS[3]:
        files = sports.get_uploaded_files()
        if files.empty:
            st.info("업로드된 파일이 없습니다.")
        else:
            st.dataframe(files, use_container_width=True, hide_index=True)
            sel = st.selectbox("대상 파일 (다시 읽기 · 삭제)", files["파일명"].tolist())
            st.caption("파서가 개선됐거나 팀·리그가 잘못 읽힌 경우, 저장돼 있는 원본으로 다시 읽을 수 있습니다. (검수 표에서 손으로 고친 값은 원본 기준으로 되돌아갑니다)")
            r1, r2 = st.columns(2)
            if r1.button("🔄 선택 파일 다시 읽기", use_container_width=True):
                ok, fail = sports.reparse_saved(sel)
                st.cache_data.clear()
                st.success(f"{ok}개 다시 읽기 완료") if ok else None
                [st.error(f) for f in fail]
            if r2.button("🔄 전체 파일 다시 읽기", use_container_width=True):
                with st.spinner("다시 읽는 중…"):
                    ok, fail = sports.reparse_saved()
                    st.cache_data.clear()
                st.success(f"{ok}개 다시 읽기 완료") if ok else None
                [st.error(f) for f in fail]

            if st.button(f"🗑️ '{sel}' 삭제", type="primary"):
                sports.delete_file_from_db(sel)
                st.cache_data.clear()
                st.rerun()
        if st.button("⚠️ 전체 초기화"):
            sports.clear_sports_db()
            st.cache_data.clear()
            st.rerun()

    if section == SECTIONS[4]:
        st.write("Streamlit Cloud 는 앱이 재시작/재배포되면 저장 파일이 사라질 수 있습니다. 주기적으로 백업하세요.")
        st.download_button("⬇️ 백업 내려받기 (zip)", sports.export_backup(), "sports_backup.zip", "application/zip")
        up_zip = st.file_uploader("백업 zip 복구", type=["zip"], key="restore")
        if up_zip and st.button("♻️ 복구 실행"):
            sports.import_backup(up_zip.getvalue())
            st.success("복구 완료")
            st.rerun()
        st.markdown("---")
        st.markdown("##### 📦 master_games.csv (누적 전체 데이터)")
        st.caption("모든 업로드를 시간순·중복 제거(날짜+리그+홈+원정+유형+기준값)해 한 파일로 만들고 R2 의 master/master_games.csv 에 저장합니다.")
        if st.button("📦 master_games.csv 만들기", key="mk_master"):
            data_, summ = sports.export_master()
            st.session_state["master"] = (data_, summ)
        if st.session_state.get("master"):
            data_, summ = st.session_state["master"]
            st.success(f"R2 마스터에 반영할 신규/추가 건수: **{summ['new']:,}건** · 전체 누적 표본 수: **{summ['total_rows']:,}행({summ['total_games']:,}경기)** · 포함된 연도 범위: **{summ['years']}**")
            st.download_button("⬇️ master_games.csv 내려받기", data_, "master_games.csv", "text/csv")


    if section == SECTIONS[5]:
        done = df[df.home_score.notna() & (df.bet_type == "일반")] if not df.empty else df
        st.write(f"**학습에 쓰이는 끝난 경기: {len(done)}경기** (경기 결과 스코어가 PDF에 있는 경기만)")
        if len(done):
            st.dataframe(done.groupby("sport").size().rename("끝난 경기 수").reset_index(), hide_index=True)
        st.caption("지난 회차 PDF를 올릴수록 팀별·종목별 보정이 쌓이고, 아래 점검의 표본이 늘어 신뢰도가 올라갑니다.")
        if len(done):
            lt = _learn_tables(ver)
            t_odds = lt["odds"]
            if not t_odds.empty:
                st.markdown("**① 배당이 가리킨 승률 vs 실제 승률 (일반, 홈승 기준)**")
                st.dataframe(t_odds, hide_index=True, use_container_width=True)
            t_h = lt["hand"]
            if not t_h.empty:
                st.markdown("**② 핸디 라인별 실제 핸디승/무/패 비율**")
                st.dataframe(t_h, hide_index=True, use_container_width=True)
            t_p = lt["payout"]
            if not t_p.empty:
                st.markdown("**③ 환급률 구간별: 인기(낮은 배당) 쪽이 실제로 이긴 비율 vs 배당이 가리킨 확률**")
                st.dataframe(t_p, hide_index=True, use_container_width=True)
            t_t = lt["trend"]
            if not t_t.empty:
                st.markdown("**④ 사이트가 표시한 배당 변동(↓하락/↑상승)이 붙은 쪽의 실제 적중률**")
                st.dataframe(t_t, hide_index=True, use_container_width=True)
            t_m = lt["move"]
            if not t_m.empty:
                st.markdown("**⑥ 내가 올린 파일로 본 배당 변화(처음→마감)와 실제 적중률**")
                st.dataframe(t_m, hide_index=True, use_container_width=True)
            t_c = lt["close"]
            if not t_c.empty:
                st.markdown("**⑦ 인기(낮은 배당) 구간별 접전·무승부 발생률 — 핸디 신호(기준점 축소 / 핸디 역배 쪽 배당 하락) 있음·없음 비교**")
                st.dataframe(t_c, hide_index=True, use_container_width=True)
                st.caption("접전 = 축구·야구 1점차 이내(축구는 무승부 포함), 농구 5점 이내, 배구 풀세트(3:2). 표본이 15경기 미만이면 [표본 부족]입니다.")
            st.caption("⑧ '같은 배당이었던 다른 경기의 승률'은 🎯 경기전 예측 카드마다 📚 줄로 표시됩니다(표본 5경기 이상이면 확률을 ±5%p 안에서 보정).")
        if len(done) >= 10 and st.button("🧪 과거 경기 적중률 점검 (그 경기 이전 데이터만으로 재예측)", use_container_width=True):
            with st.spinner("계산 중…"):
                ev, ng = sports.evaluate_finished(df)
            if ev.empty:
                st.info("점검할 수 있는 경기가 없습니다.")
            else:
                st.write(f"점검 {ng}경기 · 예측 {len(ev)}건")
                tbl = ev.groupby(["유형", "라벨"]).agg(건수=("적중", "size"), 적중률=("적중", "mean"), 평균예상확률=("예상확률", "mean")).reset_index()
                tbl["적중률"] = (tbl["적중률"] * 100).round(1).astype(str) + "%"
                tbl["평균예상확률"] = (tbl["평균예상확률"] * 100).round(1).astype(str) + "%"
                st.dataframe(tbl, hide_index=True, use_container_width=True)
                st.caption("라벨(🟢강력/🟡유력…)이 높을수록 적중률도 높아야 정상입니다. 표본이 30건 미만인 칸은 우연일 수 있습니다.")

        # ── 앱이 한 예측 vs 실제 결과 ──
        evp = _eval(ver)
        st.markdown("---")
        st.markdown("#### 🧪 앱이 미리 기록한 예측 vs 실제 결과")
        if evp.empty:
            st.info("아직 비교할 기록이 없습니다. 경기 시작 전 PDF 로 예측이 한 번 계산되면 기록되고, 이후 결과가 들어 있는 PDF 를 올리면 여기서 비교·학습됩니다.")
        else:
            k1, k2, k3 = st.columns(3)
            k1.metric("비교한 예측", f"{len(evp)}건")
            k2.metric("적중률", f"{evp.hit.mean()*100:.0f}%")
            k3.metric("평균 예상확률", f"{evp.pred_prob.mean()*100:.0f}%")
            tb = evp.groupby(["유형", "label"]).agg(건수=("hit", "size"), 적중률=("hit", "mean"), 평균예상확률=("pred_prob", "mean")).reset_index()
            tb["판정"] = ["[표본 부족 - 참고용]" if n < 15 else "" for n in tb["건수"]]
            for c in ("적중률", "평균예상확률"):
                tb[c] = (tb[c] * 100).round(1).astype(str) + "%"
            st.dataframe(tb.rename(columns={"label": "라벨"}), hide_index=True, use_container_width=True)
            st.caption("이 기록으로 구간별 (예측-실제) 차이를 계산해 이후 예측 확률을 자동 보정합니다(표본 15건 이상 구간만, 최대 ±4%p).")
            mr = sports.miss_review(evp)
            if not mr.empty:
                st.markdown("**❌ 최근 빗나간 예측과 추정 원인**")
                st.dataframe(mr, hide_index=True, use_container_width=True)
