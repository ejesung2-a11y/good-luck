import streamlit as st
import lottery_ui
import sports
import sports_ui
from datetime import datetime

st.set_page_config(
    page_title="종합 퀀텀 연구소 Pro",
    page_icon="✨",
    layout="centered"
)

sports.init_sports_db()

st.markdown("""
    <style>
    .stApp { background-color: #0f172a; color: #f8fafc; }
    .stRadio label { color: #f8fafc !important; font-weight: bold !important; font-size: 14px !important; }
    .stMarkdown p, .stCaption { color: #cbd5e1 !important; }
    .ball {
        display: inline-flex; width: 34px; height: 34px; border-radius: 50%;
        align-items: center; justify-content: center; font-weight: bold; color: white;
        margin: 0 2px; box-shadow: 0 2px 4px rgba(0,0,0,0.3); font-size: 13px;
    }
    .ball-1 { background-color: #f59e0b; }
    .ball-2 { background-color: #0284c7; }
    .ball-3 { background-color: #e11d48; }
    .ball-4 { background-color: #475569; }
    .ball-5 { background-color: #10b981; }
    .power-ball {
        display: inline-flex; width: 34px; height: 34px; border-radius: 50%;
        align-items: center; justify-content: center; font-weight: bold; color: white;
        background: linear-gradient(135deg, #ec4899, #8b5cf6); margin-left: 6px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.3); font-size: 13px; border: 2px solid #f472b6;
    }
    .result-card {
        background-color: #1e293b; padding: 12px 14px; border-radius: 12px; margin-bottom: 8px;
        display: flex; justify-content: space-between; align-items: center; border: 1px solid #334155;
    }
    .match-card {
        background-color: #1e293b; padding: 14px; border-radius: 12px; margin-bottom: 10px; border: 1px solid #334155;
    }
    .badge-soccer { background-color: #0284c7; color: white; padding: 3px 6px; border-radius: 4px; font-size: 11px; font-weight: bold; }
    .badge-baseball { background-color: #16a34a; color: white; padding: 3px 6px; border-radius: 4px; font-size: 11px; font-weight: bold; }
    .badge-basketball { background-color: #ea580c; color: white; padding: 3px 6px; border-radius: 4px; font-size: 11px; font-weight: bold; }
    .badge-volleyball { background-color: #9333ea; color: white; padding: 3px 6px; border-radius: 4px; font-size: 11px; font-weight: bold; }
    /* 세로 스크롤바: 어두운 배경에서도 잘 보이도록 항상 표시 + 굵고 밝게 */
    html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"], section.main { overflow-y: scroll !important; }
    * { scrollbar-width: auto; scrollbar-color: #94a3b8 #1e293b; }
    ::-webkit-scrollbar { width: 14px; height: 14px; }
    ::-webkit-scrollbar-track { background: #1e293b; }
    ::-webkit-scrollbar-thumb { background: #94a3b8; border-radius: 8px; border: 3px solid #1e293b; }
    ::-webkit-scrollbar-thumb:hover { background: #cbd5e1; }
    /* 오른쪽 아래 맨위/맨아래 이동 버튼 */
    .scroll-btns { position: fixed; right: 26px; bottom: 22px; z-index: 99999; display: flex; flex-direction: column; gap: 8px; }
    .scroll-btns a { background: #334155; color: #f8fafc !important; text-decoration: none !important; width: 40px; height: 40px;
        border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 18px; border: 1px solid #64748b;
        box-shadow: 0 2px 6px rgba(0,0,0,.4); }
    .scroll-btns a:hover { background: #475569; }
    .high-confidence { border-left: 5px solid #34d399; }
    .mid-confidence { border-left: 5px solid #facc15; }
    .low-confidence { border-left: 5px solid #f87171; }
    </style>
""", unsafe_allow_html=True)

st.markdown('<div id="top-anchor"></div><div class="scroll-btns"><a href="#top-anchor" target="_self" title="맨 위로">▲</a><a href="#bottom-anchor" target="_self" title="맨 아래로">▼</a></div>', unsafe_allow_html=True)
st.sidebar.markdown("### 🎲 종합 퀀텀 연구소 메뉴")
app_mode = st.sidebar.selectbox(
    "실행할 프로그램 선택",
    ["🎰 로또 6/45", "🎟️ 연금복권 720+", "⚡ 파워볼", "⚽ 스포츠 토토 배당 분석 Pro", "📥 데이터 센터(저장소·해외데이터)"]
)
st.sidebar.markdown("---")

if app_mode in ("🎰 로또 6/45", "🎟️ 연금복권 720+", "⚡ 파워볼"):
    num_games = st.sidebar.slider("🎯 생성할 게임 수", 1, 5, 1)
    mode_key = st.sidebar.selectbox("생성 방식", list(lottery_ui.MODES), format_func=lambda k: lottery_ui.MODES[k])
    fixed_nums, exclude_nums = [], []
    if app_mode == "🎰 로또 6/45":
        fixed_nums = st.sidebar.multiselect("📌 고정수 (최대 5개)", list(range(1, 46)), max_selections=5)
        exclude_nums = st.sidebar.multiselect("🚫 제외수", [n for n in range(1, 46) if n not in fixed_nums], max_selections=35)
    game = {"🎰 로또 6/45": "lotto", "🎟️ 연금복권 720+": "pension", "⚡ 파워볼": "powerball"}[app_mode]
    lottery_ui.render(game, mode_key, num_games, tuple(fixed_nums), tuple(exclude_nums))
elif app_mode == "⚽ 스포츠 토토 배당 분석 Pro":
    selected_sport = sports_ui.sidebar()
    sports_ui.render(selected_sport)
else:
    lottery_ui.data_hub()

st.markdown('<div id="bottom-anchor" style="height:60px"></div>', unsafe_allow_html=True)
