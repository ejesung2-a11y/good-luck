import random
import streamlit as st
import pandas as pd
import lottery_stats as L
import storage
import football_calib

FORMAT_HELP = {
    "lotto": "열 순서: 회차, (날짜 등 다른 열은 무시), 번호1~6, 보너스  예) 1100, 2024-01-06, 3, 12, 19, 25, 31, 44, 7",
    "powerball": "열 순서: 회차, 번호1~5(1~28), 파워볼(0~9)",
    "pension": "열 순서: 회차, 조(1~5), 당첨번호 6자리 (예: 1100, 3, 482915)",
}
MODES = {"balanced": "⚖️ 균형(과소출현 번호 소폭 가중)", "hot": "🔥 최근 30회 빈도 가중", "cold": "🧊 장기 미출현 가중"}


def _ball(n, extra=False):
    bg = "linear-gradient(135deg,#ec4899,#8b5cf6)" if extra else "#0284c7"
    return (f"<span style='display:inline-flex;width:34px;height:34px;border-radius:50%;align-items:center;justify-content:center;"
            f"font-weight:bold;color:#fff;margin:0 2px;background:{bg};font-size:13px'>{n}</span>")


def entry_panel(game, draws):
    """회차 + 당첨번호를 직접 입력해 저장(매번 파일을 올리지 않아도 됨). 저장하면 통계·번호 생성·백테스트에 바로 반영된다."""
    g = L.GAMES[game]
    nxt = (draws[-1]["round"] + 1) if draws else 1
    with st.expander(f"✍️ 회차·당첨번호 직접 입력 (현재 {len(draws)}회차 저장" + (f", 최신 {draws[-1]['round']}회)" if draws else ")"), expanded=True):
        with st.form(f"entry_{game}", clear_on_submit=True):
            if game == "pension":
                c1, c2, c3 = st.columns([1, 1, 2])
                rd = c1.number_input("회차", min_value=1, value=int(nxt), step=1)
                grp = c2.selectbox("조", [1, 2, 3, 4, 5])
                num_txt = c3.text_input("1등 당첨번호 6자리", placeholder="예: 482915", max_chars=6)
                extra_txt = ""
            else:
                c1, c2 = st.columns([1, 3])
                rd = c1.number_input("회차", min_value=1, value=int(nxt), step=1)
                if game == "lotto":
                    num_txt = c2.text_input("당첨번호 6개 (공백·쉼표로 구분)", placeholder="예: 3 12 19 25 31 44")
                    extra_txt = st.text_input("보너스 번호", placeholder="예: 7")
                else:
                    num_txt = c2.text_input("일반볼 5개 (1~28, 공백·쉼표로 구분)", placeholder="예: 3 9 14 21 27")
                    extra_txt = st.text_input("파워볼 (0~9)", placeholder="예: 4")
            submitted = st.form_submit_button("💾 저장", use_container_width=True)
        if submitted:
            if game == "pension":
                import re as _re
                nums, extra = [int(ch) for ch in _re.sub(r"\D", "", num_txt)], grp
            else:
                nums = L.parse_numbers(num_txt)
                ex = L.parse_numbers(extra_txt)
                extra = ex[0] if ex else None
            errs, existed = L.add_draw(game, int(rd), nums, extra)
            if errs:
                st.session_state["lot_err"] = errs
            else:
                st.session_state["lot_msg"] = f"{int(rd)}회 저장 완료" + (" (같은 회차를 덮어씀)" if existed else "") + (" · ☁️ R2 에 백업됨" if storage.enabled() and not storage._state["error"] else "")
            st.rerun()
        with st.expander("여러 회차를 한꺼번에 붙여넣기"):
            st.caption(FORMAT_HELP[game] + " · 한 줄에 한 회차")
            bulk = st.text_area("여러 줄 붙여넣기", height=110, key=f"bulk_{game}")
            if st.button("💾 붙여넣은 회차 저장", key=f"bulk_sv_{game}"):
                recs = L.parse_upload(game, "붙여넣기.txt", bulk.encode("utf-8"))
                if not recs:
                    st.session_state["lot_err"] = ["읽힌 회차가 없습니다. 한 줄에 '회차 번호들…' 형식인지 확인해 주세요."]
                else:
                    n_ = L.save_draws(game, recs, "직접입력(붙여넣기)")
                    st.session_state["lot_msg"] = f"{n_}회차 저장 완료"
                st.rerun()
        if draws:
            recent = list(reversed(draws[-8:]))
            show = pd.DataFrame([{"회차": d["round"], "번호": " ".join(map(str, d["nums"])) if game != "pension" else "".join(map(str, d["nums"])),
                                  {"lotto": "보너스", "powerball": "파워볼", "pension": "조"}[game]: d["extra"]} for d in recent])
            st.caption("최근 저장된 회차")
            st.dataframe(show, hide_index=True, use_container_width=True)
            d1, d2 = st.columns([2, 1])
            del_rd = d1.selectbox("잘못 입력한 회차 삭제", [d["round"] for d in recent], key=f"del_rd_{game}")
            if d2.button("🗑️ 삭제", key=f"del_btn_{game}"):
                L.delete_draw(game, del_rd)
                st.session_state["lot_msg"] = f"{del_rd}회 삭제"
                st.rerun()


def _reset_game_panel(game, n):
    """데이터 삭제 확인/취소 안내"""
    if not st.session_state.get(f"lot_confirm_{game}"):
        return
    g = L.GAMES[game]
    n_orig = len([1 for rel, _ in storage.list_keys() if rel.startswith(f"uploads/{game}/")]) if storage.enabled() else 0
    st.error(f"⚠️ **정말 {g['name']} 데이터를 삭제하시겠습니까?** 저장된 {n}회차가 삭제되고, R2 의 DB 에도 반영됩니다.")
    opt_db = "저장된 회차만 삭제 — 업로드한 원본 파일은 R2 에 둠"
    opt_all = f"저장된 회차 + 업로드 원본 파일(R2 {n_orig}개)까지 모두 삭제 (되돌릴 수 없음)"
    mode = st.radio("삭제 범위", [opt_db, opt_all], key=f"lot_mode_{game}")
    c1, c2 = st.columns(2)
    if c1.button("✅ 확인 — 삭제 실행", type="primary", use_container_width=True, key=f"lot_ok_{game}"):
        out = L.reset_game(game, delete_originals=(mode == opt_all))
        st.session_state.pop(f"lot_confirm_{game}", None)
        st.session_state["lot_msg"] = f"{g['name']} {out['draws']}회차 삭제 완료" + (f" · 원본 {out['originals']}개 삭제" if mode == opt_all else "")
        st.rerun()
    if c2.button("❌ 취소", use_container_width=True, key=f"lot_no_{game}"):
        st.session_state.pop(f"lot_confirm_{game}", None)
        st.rerun()


def upload_panel(game):
    g = L.GAMES[game]
    draws = L.load_draws(game)
    entry_panel(game, draws)
    with st.expander(f"📥 {g['name']} 과거 당첨번호 파일 업로드 (엑셀·CSV·PDF)", expanded=False):
        sm = L.summary(draws)
        if sm:
            st.info(f"저장됨: {sm['n']}회차 ({sm['first']}~{sm['last']}회)" + (f" · ⚠️ 누락 {len(sm['missing'])}개 회차: {sm['missing'][:15]}{'…' if len(sm['missing']) > 15 else ''}" if sm["missing"] else " · 누락 없음 ✅"))
        st.caption(FORMAT_HELP[game] + " · CSV / 엑셀(xlsx) / 인쇄해서 저장한 PDF / 표를 복사한 txt 모두 가능 · 같은 회차는 최근 업로드로 덮어씁니다.")
        ver = st.session_state.get(f"lot_ver_{game}", 0)
        up = st.file_uploader("파일 선택", type=["csv", "xlsx", "xls", "txt", "pdf"], accept_multiple_files=True, key=f"up_{game}_{ver}")
        if up and st.button("💾 읽어서 저장", key=f"sv_{game}", use_container_width=True):
            tot, errs = 0, []
            for f in up:
                raw = f.getvalue()
                recs = L.parse_upload(game, f.name, raw)
                if not recs:
                    errs.append(f"{f.name}: 읽힌 회차가 없습니다. 열 순서를 확인해 주세요.")
                    continue
                tot += L.save_draws(game, recs, f.name)
                L.save_raw_upload(game, f.name, raw)
            if errs:
                st.session_state["lot_err"] = errs
            if up:
                st.session_state[f"lot_ver_{game}"] = ver + 1   # 처리 끝 → 업로드 칸 비우기
            if tot:
                st.session_state["lot_msg"] = f"{tot}회차 저장 완료" + (" · ☁️ R2 에 백업됨" if storage.enabled() and not storage._state["error"] else "")
            st.rerun()
    if draws:
        if st.button("🗑️ 이 게임 데이터 삭제 …", key=f"clr_{game}"):
            st.session_state[f"lot_confirm_{game}"] = True
            st.rerun()
        _reset_game_panel(game, len(draws))
    return draws


def render(game, mode="balanced", n_games=1, fixed=(), exclude=()):
    g = L.GAMES[game]
    st.markdown(f"<h2 style='text-align:center;color:white'>🎲 {g['name']}</h2>", unsafe_allow_html=True)
    draws = upload_panel(game)
    for e in st.session_state.pop("lot_err", []):
        st.error(e)
    if st.session_state.get("lot_msg"):
        st.success(st.session_state.pop("lot_msg"))
    if storage.enabled() and storage._state["error"]:
        st.error("☁️ R2 저장 오류: " + storage._state["error"])
    if not draws:
        st.info("과거 당첨번호를 올리면 그 데이터로 통계를 계산합니다. 없으면 완전 무작위로 생성됩니다.")
    t1, t2, t3 = st.tabs(["🎯 번호 생성", "📊 통계 보기", "🧪 백테스트(무작위와 비교)"])

    with t1:
        if st.button(f"🚀 {n_games}게임 생성", use_container_width=True, key=f"gen_{game}"):
            for label in "ABCDE"[:n_games]:
                r = L.generate(game, draws, mode, fixed, exclude)
                if game == "pension":
                    body = f"<b style='color:#facc15'>{r['extra']}조</b> " + "".join(_ball(d) for d in r["nums"])
                else:
                    body = "".join(_ball(n) for n in r["nums"]) + (f" + {_ball(r['extra'], True)}" if r["extra"] is not None else "")
                st.markdown(f"<div style='background:#1e293b;border:1px solid #334155;border-radius:12px;padding:10px 12px;margin-bottom:6px'>"
                            f"<b style='color:#818cf8;margin-right:8px'>{label}</b>{body}"
                            f"<div style='color:#94a3b8;font-size:11px;margin-top:4px'>{r['why']}</div></div>", unsafe_allow_html=True)
            st.caption("⚠️ 추첨은 독립 사건입니다. 통계는 번호의 '모양'을 고를 뿐 당첨 확률을 바꾸지 못합니다 (아래 백테스트 참고).")

    with t2:
        if not draws:
            st.info("데이터 없음")
        else:
            s = L.stats(game, draws, 30)
            if game == "pension":
                for i, c in enumerate(s["pos"]):
                    st.write(f"{i+1}번째 자리", {d: c.get(d, 0) for d in range(10)})
            else:
                lo, hi = g["lo"], g["hi"]
                df = pd.DataFrame({"번호": range(lo, hi + 1),
                                   "전체 출현": [s["freq_all"].get(v, 0) for v in range(lo, hi + 1)],
                                   "최근30회": [s["freq_rec"].get(v, 0) for v in range(lo, hi + 1)],
                                   "미출현 회차수": [s["gap"][v] for v in range(lo, hi + 1)]})
                st.bar_chart(df.set_index("번호")["전체 출현"])
                st.dataframe(df, hide_index=True, use_container_width=True, height=260)
                top = s["pairs"].most_common(10)
                st.write("동반 출현 TOP10", pd.DataFrame([{"번호쌍": f"{a}-{b}", "함께 나온 횟수": c} for (a, b), c in top]))
                st.caption(f"합계 10~90% 구간 {s['sum_lo']}~{s['sum_hi']} · 흔한 홀수 개수 {sorted(s['odd_range'])}")

    with t3:
        if len(draws) < 60:
            st.info("백테스트에는 최소 60회차 이상이 필요합니다.")
        else:
            st.write("과거 각 회차 **직전까지의 데이터만** 써서 번호를 만들고 실제 당첨번호와 맞춘 개수를 무작위 선택과 비교합니다.")
            if st.button("▶ 백테스트 실행", key=f"bt_{game}"):
                r = L.backtest(game, draws, mode)
                if r:
                    st.metric("내 방식 평균 일치", f"{r['my_mean']:.3f}")
                    st.metric("무작위 평균 일치", f"{r['rnd_mean']:.3f}")
                    verdict = "무작위와 통계적으로 구별되지 않음" if abs(r["z"]) < 2 else ("무작위보다 높게 나왔으나 반복 검증 필요(우연 가능)" if r["z"] > 0 else "무작위보다 낮음")
                    st.write(f"검증 {r['rounds']}회차 · z={r['z']:.2f} → **{verdict}**")


def data_hub():
    st.markdown("<h2 style='text-align:center;color:white'>📥 데이터 센터</h2>", unsafe_allow_html=True)
    ok, msg = storage.status()
    (st.success if ok else st.warning)(("☁️ " if ok else "⚠️ ") + msg)
    if not ok:
        st.code('''# .streamlit/secrets.toml  (Streamlit Cloud: Settings → Secrets)
[r2]
account_id = "여기에 대시보드 주소의 긴 해시"
access_key_id = "R2 API 토큰의 Access Key ID"
secret_access_key = "R2 API 토큰의 Secret"
bucket = "ajin-info-files"
prefix = "quantum-lab/"''', language="toml")
    if ok and st.button("☁️ 지금 R2로 전체 동기화(덮어쓰기)", use_container_width=True):
        st.success(f"{storage.push_all(force=True)}개 파일을 R2 에 올렸습니다.")
    if ok:
        keys = storage.list_keys()
        if keys:
            st.dataframe(pd.DataFrame(keys, columns=["파일(폴더 기준)", "크기(byte)"]), hide_index=True, use_container_width=True, height=200)

    if ok:
        st.markdown("### ♻️ R2 원본으로 데이터 복구")
        st.caption("앱 DB 가 비었지만 R2 에 올려 둔 원본(토토 PDF, 로또·연금·파워볼 파일)이 남아 있을 때 다시 읽어 복구합니다. 다시 올릴 필요가 없습니다.")
        if st.button("♻️ 토토 + 로또/연금/파워볼 전부 복구", use_container_width=True):
            import sports
            bar = st.progress(0.0)
            n_ok, skipped, fail = sports.rebuild_from_storage(bar.progress)
            lot = L.rebuild_from_storage()
            st.cache_data.clear()
            st.success(f"토토 {n_ok}개 파일 복구(이미 있던 {skipped}개 제외) · 로또 {lot['lotto']}회차 · 연금 {lot['pension']}회차 · 파워볼 {lot['powerball']}회차" + (f" · 실패 {len(fail)}: {fail[:3]}" if fail else ""))
    st.markdown("### ⚽ 해외 축구 과거 경기 CSV (배당 + 결과)")
    cal = football_calib.load()
    if cal:
        st.info(f"현재 보정표: {cal['n']:,}경기 · 출처 {cal['source']} · 사용 열 {cal['columns']}")
    c1, c2 = st.columns([3, 1])
    gh = c1.text_input("GitHub CSV 주소", value=football_calib.GITHUB_PRESET, label_visibility="collapsed")
    if c2.button("🐙 가져오기", use_container_width=True):
        with st.spinner("내려받는 중(약 45MB)…"):
            try:
                out = football_calib.import_from_github(gh)
                st.success(f"{out['n']:,}경기로 보정표 생성 완료")
                st.rerun()
            except Exception as e:
                st.error(f"가져오기 실패: {e}")
    st.caption("기본값은 xgabora/Club-Football-Match-Data(MIT 라이선스, 약 23만 경기, 배당+결과). 다른 CSV 주소도 가능합니다.")
    st.caption("또는 직접 받은 CSV 업로드 — GitHub(xgabora/Club-Football-Match-Data 등)·football-data.co.uk·FootyStats 에서 받은 CSV. 홈/무/원정 배당과 결과(H/D/A) 열이 있어야 합니다.")
    fv = st.session_state.get("fb_ver", 0)
    f = st.file_uploader("CSV 업로드", type=["csv"], key=f"fb_csv_{fv}")
    if f and st.button("📐 보정표 만들기", use_container_width=True):
        try:
            out = football_calib.build_from_csv(f.name, f.getvalue())
            st.session_state["fb_ver"] = fv + 1
            st.success(f"{out['n']:,}경기로 보정표 생성 완료")
            st.rerun()
        except Exception as e:
            st.error(str(e))
