import os
import html
import streamlit as st
import pandas as pd
import sports
from datetime import timedelta

BADGE = {"축구": "#0284c7", "야구": "#16a34a", "농구": "#ea580c", "배구": "#9333ea"}
LABEL_BORDER = {"강력": "#34d399", "유력": "#facc15", "낮음": "#fb923c", "위험": "#f87171"}


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
                df=sports.parse_text(text), raw=raw, path=sports.stash_raw(f.name, raw), method=method,
                round=round_in or sports.round_from_name(f.name) or sports.guess_round_label(text))
        if pasted.strip():
            name = f"붙여넣기_{sports.now_kst():%Y%m%d_%H%M%S}.txt"
            st.session_state["pending"][name] = dict(
                df=sports.parse_text(pasted), raw=pasted.encode(), path=sports.stash_raw(name, pasted.encode()), method="텍스트 붙여넣기",
                round=round_in or sports.guess_round_label(pasted))
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
    edited, labels = {}, {}
    for name, p in list(pend.items()):
        df = p["df"]
        with st.expander(f"📝 {name}  ({p['method']}, {len(df)}행)", expanded=len(pend) == 1):
            if df.empty:
                st.error("읽힌 경기가 없습니다. 텍스트 붙여넣기를 사용하거나 해상도가 높은 캡처를 올려 주세요.")
                continue
            bad = (~df.apply(lambda r: sports.odds_ok(r), axis=1) & df.odds1.notna() & (df.bet_type != "SUM")).sum()
            if p["method"].startswith("OCR"):
                st.warning(f"OCR 은 숫자를 틀리게 읽을 수 있습니다. 배당이 비정상인 {bad}행은 예측에서 자동 제외됩니다. 표에서 직접 고친 뒤 저장하세요.")
            labels[name] = st.text_input("회차 (파일명에서 자동 · 같은 회차가 여러 개면 저장 시 -1, -2 로 구분)", value=p["round"], key=f"rl_{name}")
            edited[name] = st.data_editor(df, num_rows="dynamic", use_container_width=True, key=f"ed_{name}", height=300)
    with top:
        st.markdown(f"**읽은 파일 {len(pend)}개 · 합계 {sum(len(p['df']) for p in pend.values())}행** — 검수가 끝났으면 한 번에 저장하세요.")
        c1, c2 = st.columns(2)
        if c1.button(f"💾 {len(edited)}개 파일 일괄 저장 (같은 경기는 최신으로 덮어쓰기)", type="primary", use_container_width=True, disabled=not edited):
            final = sports.resolve_round_labels({n: (labels[n] or "미지정") for n in edited})
            for n, df_ in edited.items():
                sports.save_matches(df_, n, final[n], saved_path=pend[n].get("path"))
            st.session_state["pending"] = {}
            st.session_state["save_msg"] = f"{len(edited)}개 파일 저장 완료 → " + ", ".join(f"{n} = {final[n]}" for n in edited)
            st.rerun()
        if c2.button("전체 버리기", use_container_width=True):
            for n, p in pend.items():
                if p.get("path") and os.path.exists(p["path"]):
                    os.remove(p["path"]); sports.storage.remove(sports._rel(p["path"]))
            st.session_state["pending"] = {}
            st.rerun()


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
    out = []
    for o, t in ((r["odds1"], r.get("trend1")), (r["oddsx"], r.get("trendx")), (r["odds2"], r.get("trend2"))):
        if pd.notna(o):
            arrow = "" if t is None or pd.isna(t) or int(t) == 0 else ("<span style='color:#38bdf8'>↓</span>" if int(t) < 0 else "<span style='color:#f87171'>↑</span>")
            out.append(f"{o:g}{arrow}")
    return " / ".join(out) or "미발매"


def _card(g):
    head = g.iloc[0]
    color = BADGE.get(head["sport"], "#475569")
    parts = [f"""<span style="background:{color};color:#fff;padding:2px 6px;border-radius:4px;font-size:11px">{head['sport']} · {html.escape(str(head['league']))}</span>
      <b style="color:#38bdf8;margin-left:6px">{head['match_datetime'][5:16]}</b>
      <div style="font-size:16px;font-weight:bold;margin:6px 0">🏠 {html.escape(str(head['home_team']))} <span style="color:#64748b;font-size:12px">vs</span> ✈️ {html.escape(str(head['away_team']))}</div>"""]
    for _, r in g.iterrows():
        bt = r["bet_type"]
        tname = {"일반": "일반", "H": f"핸디 {r['line']:+g}" if pd.notna(r["line"]) else "핸디",
                 "U": f"언더오버 {r['line']:g}" if pd.notna(r["line"]) else "언더오버", "SUM": "홀짝"}.get(bt, bt)
        prob = f"{r['pred_prob']*100:.0f}%" if pd.notna(r["pred_prob"]) else "-"
        row = [f"<div style='border-top:1px solid #334155;padding-top:6px;margin-top:6px'><b>{tname}</b> · 배당 {_odds_txt(r)} → <b style='color:#facc15'>{html.escape(str(r['pred_pick']))}</b> ({prob}) {r['pred_label']} · 스코어 {html.escape(str(r['pred_score']))}</div>"]
        row.append(_bar(r.get("pred_dist")))
        chips = []
        if r.get("pred_alt"): chips.append(f"대안 {r['pred_alt']}")
        if r.get("pred_draw"): chips.append(r["pred_draw"])
        if r.get("pred_upset_txt"): chips.append(r["pred_upset_txt"])
        if r.get("pred_signal"): chips.append(r["pred_signal"])
        if chips:
            row.append(f"<div style='font-size:12px;color:#e2e8f0'>{' · '.join(html.escape(str(c)) for c in chips)}</div>")
        row.append(f"<div style='font-size:11px;color:#94a3b8;margin-top:2px'>🔍 {html.escape(str(r['pred_why']))}</div>")
        if r.get("pred_hist"):
            row.append(f"<div style='font-size:11px;color:#a5b4fc'>{html.escape(str(r['pred_hist'])).replace(chr(10), '<br>')}</div>")
        parts.append("".join(row))
    st.markdown(f"<div style='background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px;margin-bottom:10px'>{''.join(parts)}</div>", unsafe_allow_html=True)


TYPE_ORDER = {"일반": 0, "승": 1, "H": 2, "U": 3, "SUM": 4}


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
        if bt == "일반": tname = "일반"
        elif bt == "H": tname = f"핸디 {r['line']:+g}" if pd.notna(r["line"]) else "핸디"
        elif bt == "U": tname = f"언더오버 {r['line']:g}" if pd.notna(r["line"]) else "언더오버"
        elif bt == "SUM": tname = "홀짝"
        else: tname = bt
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
      <b style="color:#38bdf8;margin-left:6px">{head['match_datetime'][5:16]}</b>{score_html}
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
        st.rerun()
    pending_review()
    if sports.storage.enabled() and sports.storage._state["error"]:
        st.error("☁️ R2 저장 오류: " + sports.storage._state["error"])

    df = sports.load_matches()
    t1, tc, t2, t3, t4, t5 = st.tabs(["🎯 경기전 예측", "🧩 조합·이변", "📜 지난 경기", "📂 파일 관리", "💾 백업/복구", "📈 학습 현황"])

    # 핸디·언더오버 행까지 함께 있어야 교차확인(핸디무→일반승/무)이 되므로, 전체 행으로 예측한 뒤 화면에서 걸러낸다
    pr_all = pd.DataFrame()
    if not df.empty:
        up_all = df[pd.to_datetime(df.match_datetime) > now]
        if selected_sport != "전체":
            up_all = up_all[up_all.sport == selected_sport]
        pr_all = sports.build_predictions(df, up_all)

    with t1:
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
                st.info("현재 시각 이후의 경기가 없습니다. 새 회차 캡처를 업로드하세요.")
            else:
                for _, g in pr.groupby(["match_datetime", "league", "home_team", "away_team"], sort=False):
                    _card(g)

    with tc:
        if pr_all.empty:
            st.info("경기전 데이터가 없습니다.")
        else:
            st.markdown("#### 🎲 이변(역배) 후보 · 일반 경기")
            ge = pr_all[(pr_all.bet_type == "일반") & pr_all.pred_upset.notna() & pr_all.pred_dist.notna()].copy()
            if not ge.empty:
                ge["역배확률"] = (ge.pred_upset * 100).round(0).astype(int)
                ge["무확률"] = ge.pred_dist.map(lambda d: round(dict(d).get("무", 0) * 100) if isinstance(d, list) and "무" in dict(d) else None)
                ge["경기"] = ge.home_team + " vs " + ge.away_team
                ge["예상"] = ge.pred_pick + " (" + (ge.pred_prob * 100).round(0).astype(int).astype(str) + "%)"
                cols = ["match_datetime", "league", "경기", "예상", "역배확률", "무확률", "pred_upset_txt", "pred_signal"]
                top = ge.sort_values("역배확률", ascending=False).head(10)[cols]
                top.columns = ["시각", "리그", "경기", "예상", "역배%", "무%", "판정", "배당변동"]
                st.dataframe(top, hide_index=True, use_container_width=True)
            st.markdown("#### 🧩 조합 추천")
            n_legs = st.slider("조합 경기 수", 2, 8, 4)
            st.caption("한 경기에서는 한 가지 선택만, 서로 다른 경기로 구성합니다. 확률은 경기끼리 독립이라고 가정한 값이며, 기대환급 = 합산확률 × 합산배당 입니다.")
            for stl, cfg in sports.STYLES.items():
                c = sports.build_combos(pr_all, n_legs, stl)
                with st.expander(f"{stl} — {cfg['desc']}", expanded=stl == "안전형"):
                    if not c:
                        st.info("조건에 맞는 경기가 부족합니다.")
                        continue
                    k1, k2, k3 = st.columns(3)
                    k1.metric("합산 적중확률", f"{c['joint_p']*100:.1f}%")
                    k2.metric("합산 배당", f"{c['joint_odds']:.2f}배")
                    k3.metric("기대환급", f"{c['ev']*100:.0f}%")
                    for l in c["legs"]:
                        tn = {"일반": "일반", "H": f"핸디 {l['line']:+g}" if pd.notna(l["line"]) else "핸디", "U": f"언더오버 {l['line']:g}" if pd.notna(l["line"]) else "언더오버"}.get(l["bet_type"], l["bet_type"])
                        st.markdown(f"- **{l['home_team']} vs {l['away_team']}** · {tn} → **{l['pred_pick']}** (배당 {l['_odds']:g}, 확률 {l['pred_prob']*100:.0f}%) {l['pred_label']}")
            st.warning("기대환급이 100%를 넘는 조합은 드뭅니다. 이 프로그램의 확률은 배당과 과거 기록에서 나온 값이라 시장보다 크게 정확하다고 보장할 수 없습니다. 선수·감독 상태와 경기 환경은 반영하지 못합니다.")

    with t2:
        if df.empty:
            st.info("데이터 없음")
        else:
            mode = st.radio("조회 기준", ["날짜", "회차"], horizontal=True)
            if mode == "날짜":
                ds = sorted({x[:10] for x in df.match_datetime}, reverse=True)
                key = st.selectbox("날짜 선택", ds)
                sub = df[df.match_datetime.str.startswith(key)]
            else:
                key = st.selectbox("회차 선택", sorted(df.round_label.dropna().unique(), reverse=True))
                sub = df[df.round_label == key]
            if selected_sport != "전체":
                sub = sub[sub.sport == selected_sport]
            keys = ["match_datetime", "league", "home_team", "away_team"]
            ngames = sub[keys].drop_duplicates().shape[0]
            st.caption(f"{ngames}경기 · 카드 안의 초록색 배당이 실제 결과입니다(최종 스코어가 있는 경기만).")
            for _, g in sub.sort_values(["match_datetime", "game_no"]).groupby(keys, sort=False):
                _past_card(g)
            with st.expander("📋 원본 표로 보기"):
                st.dataframe(sub[["game_no", "match_datetime", "league", "bet_type", "line", "home_team", "away_team",
                                  "odds1", "oddsx", "odds2", "home_score", "away_score"]],
                             use_container_width=True, hide_index=True)

    with t3:
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
                st.success(f"{ok}개 다시 읽기 완료") if ok else None
                [st.error(f) for f in fail]
            if r2.button("🔄 전체 파일 다시 읽기", use_container_width=True):
                with st.spinner("다시 읽는 중…"):
                    ok, fail = sports.reparse_saved()
                st.success(f"{ok}개 다시 읽기 완료") if ok else None
                [st.error(f) for f in fail]

            if st.button(f"🗑️ '{sel}' 삭제", type="primary"):
                sports.delete_file_from_db(sel)
                st.rerun()
        if st.button("⚠️ 전체 초기화"):
            sports.clear_sports_db()
            st.rerun()

    with t4:
        st.write("Streamlit Cloud 는 앱이 재시작/재배포되면 저장 파일이 사라질 수 있습니다. 주기적으로 백업하세요.")
        st.download_button("⬇️ 백업 내려받기 (zip)", sports.export_backup(), "sports_backup.zip", "application/zip")
        up_zip = st.file_uploader("백업 zip 복구", type=["zip"], key="restore")
        if up_zip and st.button("♻️ 복구 실행"):
            sports.import_backup(up_zip.getvalue())
            st.success("복구 완료")
            st.rerun()


    with t5:
        done = df[df.home_score.notna() & (df.bet_type == "일반")] if not df.empty else df
        st.write(f"**학습에 쓰이는 끝난 경기: {len(done)}경기** (경기 결과 스코어가 PDF에 있는 경기만)")
        if len(done):
            st.dataframe(done.groupby("sport").size().rename("끝난 경기 수").reset_index(), hide_index=True)
        st.caption("지난 회차 PDF를 올릴수록 팀별·종목별 보정이 쌓이고, 아래 점검의 표본이 늘어 신뢰도가 올라갑니다.")
        if len(done):
            hist = sports.history_table(df)
            t_odds = sports.odds_winrate_table(hist)
            if not t_odds.empty:
                st.markdown("**① 배당이 가리킨 승률 vs 실제 승률 (일반, 홈승 기준)**")
                st.dataframe(t_odds, hide_index=True, use_container_width=True)
            t_h = sports.handicap_winrate_table(hist)
            if not t_h.empty:
                st.markdown("**② 핸디 라인별 실제 핸디승/무/패 비율**")
                st.dataframe(t_h, hide_index=True, use_container_width=True)
            t_p = sports.payout_table(hist)
            if not t_p.empty:
                st.markdown("**③ 환급률 구간별: 인기(낮은 배당) 쪽이 실제로 이긴 비율 vs 배당이 가리킨 확률**")
                st.dataframe(t_p, hide_index=True, use_container_width=True)
            t_t = sports.trend_table(hist)
            if not t_t.empty:
                st.markdown("**④ 사이트가 표시한 배당 변동(↓하락/↑상승)이 붙은 쪽의 실제 적중률**")
                st.dataframe(t_t, hide_index=True, use_container_width=True)
            st.caption("⑤ '같은 배당이었던 다른 경기의 승률'은 🎯 경기전 예측 카드마다 📚 줄로 표시됩니다(표본 5경기 이상이면 확률을 ±5%p 안에서 보정).")
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
