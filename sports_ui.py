import os
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


def _card(g):
    sport = g.iloc[0]["sport"]
    color = BADGE.get(sport, "#475569")
    head = g.iloc[0]
    st.markdown(f"""<div style="background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px;margin-bottom:6px">
      <span style="background:{color};color:#fff;padding:2px 6px;border-radius:4px;font-size:11px">{sport} · {head['league']}</span>
      <b style="color:#38bdf8;margin-left:6px">{head['match_datetime'][5:16]}</b>
      <div style="font-size:16px;font-weight:bold;margin-top:6px">🏠 {head['home_team']} <span style="color:#64748b;font-size:12px">vs</span> {head['away_team']}</div></div>""",
                unsafe_allow_html=True)
    for _, r in g.iterrows():
        tname = {"일반": "일반", "H": f"핸디 {r['line']:+g}" if pd.notna(r["line"]) else "핸디",
                 "U": f"언더오버 {r['line']:g}" if pd.notna(r["line"]) else "언더오버", "SUM": "홀짝"}.get(r["bet_type"], r["bet_type"])  # 승N패 는 그대로 표시
        label = r["pred_label"]
        prob = f"{r['pred_prob']*100:.0f}%" if pd.notna(r["pred_prob"]) else "-"
        odds = " / ".join(f"{x:g}" for x in [r["odds1"], r["oddsx"], r["odds2"]] if pd.notna(x)) or "미발매"
        up = f" · 역배 가능 {r['pred_upset']*100:.0f}%" if pd.notna(r.get("pred_upset")) and r["pred_upset"] else ""
        st.markdown(f"**{tname}** · 배당 {odds} → **{r['pred_pick']}** ({prob}) {label} · 스코어 {r['pred_score']}{up}")
        st.caption("🔍 " + r["pred_why"])
        if r.get("pred_hist"):
            st.caption(r["pred_hist"])


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
    t1, t2, t3, t4, t5 = st.tabs(["🎯 경기전 예측", "📜 지난 경기", "📂 파일 관리", "💾 백업/복구", "📈 학습 현황"])

    with t1:
        if df.empty:
            st.info("저장된 경기가 없습니다. 좌측에서 업로드하세요.")
        else:
            dts = pd.to_datetime(df.match_datetime)
            up = df[dts > now]
            days = sorted({d.date() for d in pd.to_datetime(up.match_datetime)})
            pick_day = st.selectbox("날짜", ["오늘 이후 전부"] + [d.strftime("%Y-%m-%d") for d in days])
            if pick_day != "오늘 이후 전부":
                up = up[up.match_datetime.str.startswith(pick_day)]
            if selected_sport != "전체":
                up = up[up.sport == selected_sport]
            bt = st.radio("유형", ["전체", "일반", "핸디(H)", "언더오버(U)"], horizontal=True)
            up = up[{"전체": up.bet_type.notna(), "일반": up.bet_type == "일반", "핸디(H)": up.bet_type == "H",
                     "언더오버(U)": up.bet_type == "U"}[bt]]
            pr = sports.build_predictions(df, up)
            st.caption(f"경기전 {pr[['match_datetime','home_team','away_team']].drop_duplicates().shape[0] if not pr.empty else 0}경기 · 누적 DB {len(df)}행")
            if pr.empty:
                st.info("현재 시각 이후의 경기가 없습니다. 새 회차 캡처를 업로드하세요.")
            else:
                for _, g in pr.groupby(["match_datetime", "league", "home_team", "away_team"], sort=False):
                    _card(g)

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
            st.dataframe(sub[["game_no", "match_datetime", "league", "bet_type", "line", "home_team", "away_team",
                              "odds1", "oddsx", "odds2", "home_score", "away_score"]],
                         use_container_width=True, hide_index=True)

    with t3:
        files = sports.get_uploaded_files()
        if files.empty:
            st.info("업로드된 파일이 없습니다.")
        else:
            st.dataframe(files, use_container_width=True, hide_index=True)
            sel = st.selectbox("삭제할 파일", files["파일명"].tolist())
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
            st.caption("③ '같은 배당이었던 다른 경기의 승률'은 🎯 경기전 예측 카드마다 📚 줄로 표시됩니다(표본 5경기 이상이면 확률을 ±5%p 안에서 보정).")
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
