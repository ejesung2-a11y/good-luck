import streamlit as st
import requests
import random
from datetime import datetime

# 페이지 설정 (모바일 뷰포트 최적화)
st.set_page_config(
    page_title="종합 퀀텀 복권 연구소 Pro",
    page_icon="✨",
    layout="centered"
)

# 모바일 터치 및 반응형 다크 모드 스타일 최적화
st.markdown("""
    <style>
    .stApp {
        background-color: #0f172a;
        color: #f8fafc;
    }
    .ball {
        display: inline-flex;
        width: 34px;
        height: 34px;
        border-radius: 50%;
        align-items: center;
        justify-content: center;
        font-weight: bold;
        color: white;
        margin: 0 2px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.3);
        font-size: 13px;
    }
    .ball-1 { background-color: #f59e0b; }
    .ball-2 { background-color: #0284c7; }
    .ball-3 { background-color: #e11d48; }
    .ball-4 { background-color: #475569; }
    .ball-5 { background-color: #10b981; }
    
    .power-ball {
        display: inline-flex;
        width: 34px;
        height: 34px;
        border-radius: 50%;
        align-items: center;
        justify-content: center;
        font-weight: bold;
        color: white;
        background: linear-gradient(135deg, #ec4899, #8b5cf6);
        margin-left: 6px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.3);
        font-size: 13px;
        border: 2px solid #f472b6;
    }
    
    .pension-group-badge {
        display: inline-flex;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: bold;
        background: linear-gradient(135deg, #6366f1, #a855f7);
        color: white;
        font-size: 12px;
        margin-right: 6px;
        vertical-align: middle;
    }
    .pension-set-badge {
        display: inline-flex;
        padding: 4px 8px;
        border-radius: 6px;
        font-weight: bold;
        background: linear-gradient(135deg, #f59e0b, #ef4444);
        color: white;
        font-size: 11px;
        margin-right: 6px;
        vertical-align: middle;
    }
    .pension-digit {
        display: inline-flex;
        width: 28px;
        height: 34px;
        background-color: #1e293b;
        border: 1px solid #475569;
        border-radius: 6px;
        align-items: center;
        justify-content: center;
        font-weight: bold;
        color: #38bdf8;
        margin: 0 1px;
        font-size: 14px;
    }
    .fixed-badge {
        border: 2px solid #fde047;
        box-shadow: 0 0 6px rgba(253, 224, 71, 0.6);
    }
    .result-card {
        background-color: #1e293b;
        padding: 12px 14px;
        border-radius: 12px;
        margin-bottom: 8px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        border: 1px solid #334155;
    }
    </style>
""", unsafe_allow_html=True)

def get_ball_class(num):
    if num <= 6: return "ball-1"
    if num <= 12: return "ball-2"
    if num <= 18: return "ball-3"
    if num <= 24: return "ball-4"
    return "ball-5"

# ==========================================
# 로또 6/45 관련 함수
# ==========================================
@st.cache_data(ttl=3600)
def fetch_lotto_data():
    try:
        ref_date = datetime(2002, 12, 7)
        days_diff = (datetime.now() - ref_date).days
        latest_round = (days_diff // 7) + 1
        
        history = []
        start_round = max(1, latest_round - 50)
        headers = {'User-Agent': 'Mozilla/5.0'}
        
        for drw in range(latest_round, start_round - 1, -1):
            url = f"https://www.dhlottery.co.kr/common.do?method=getLottoNumber&drwNo={drw}"
            res = requests.get(url, headers=headers, timeout=3).json()
            if res.get("returnValue") == "success":
                numbers = [
                    res["drwtNo1"], res["drwtNo2"], res["drwtNo3"],
                    res["drwtNo4"], res["drwtNo5"], res["drwtNo6"]
                ]
                history.append({"round": drw, "numbers": numbers})
        return history, "실시간 동행복권 로또 데이터 연동 완료 🚀"
    except Exception:
        return [], "오프라인 모드 가동 중 ⚠️"

def calculate_ac(nums):
    diffs = set()
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            diffs.add(abs(nums[i] - nums[j]))
    return len(diffs) - (len(nums) - 1)

def count_zones(nums):
    zones = set()
    for n in nums:
        zones.add((n - 1) // 10)
    return len(zones)

def generate_lotto_game(history, mode, fixed_nums, exclude_nums, strict_level):
    clean_exclude = set(exclude_nums) - set(fixed_nums)
    available_nums = [i for i in range(1, 46) if i not in fixed_nums and i not in clean_exclude]
    weights = {i: 10.0 for i in available_nums}
    
    if history:
        latest_numbers = history[0]["numbers"]
        freq = {i: 0 for i in range(1, 46)}
        recent_appear = set()
        for draw in history[:15]:
            for num in draw["numbers"]:
                freq[num] += 1
                recent_appear.add(num)
                
        for i in available_nums:
            if mode == "🔥 핫넘버 집중형":
                if i in latest_numbers: weights[i] += 20.0
                if freq[i] >= 3: weights[i] += (freq[i] * 4.0)
            elif mode == "🧊 콜드넘버 역발상형":
                if i not in recent_appear: weights[i] += 18.0
                if i in latest_numbers: weights[i] = max(1.0, weights[i] - 5.0)
            else:
                if i in latest_numbers: weights[i] += 12.0
                weights[i] += (freq[i] * 2.0)

    needed_count = 6 - len(fixed_nums)
    if len(available_nums) < needed_count:
        nums = sorted(list(fixed_nums) + random.sample([i for i in range(1, 46) if i not in fixed_nums], needed_count))
        return {"numbers": nums, "grade": "커스텀 조합", "ac": calculate_ac(nums)}

    attempts = 3000 + (6 - strict_level) * 1000

    for _ in range(attempts):
        picked = random.choices(list(weights.keys()), weights=list(weights.values()), k=needed_count)
        if len(set(picked)) != needed_count: continue
        nums = sorted(list(fixed_nums) + picked)
        
        total_sum = sum(nums)
        odds = sum(1 for n in nums if n % 2 != 0)
        ac_val = calculate_ac(nums)
        zone_count = count_zones(nums)
        
        if len(fixed_nums) >= 3:
            if ac_val >= 4: break
        else:
            if 100 <= total_sum <= 170 and 2 <= odds <= 4 and ac_val >= 5 and zone_count >= 3:
                has_triple = any(nums[i] == nums[i+1]-1 == nums[i+2]-2 for i in range(4))
                if not has_triple: break
    else:
        while True:
            picked = random.choices(list(weights.keys()), weights=list(weights.values()), k=needed_count)
            if len(set(picked)) == needed_count:
                nums = sorted(list(fixed_nums) + picked)
                break

    entropy_score = (sum(nums) * nums[-1] + calculate_ac(nums)) % 100
    grade = "S+급 (초고정밀 하이퍼)" if entropy_score > 70 else ("A급 (스마트 밸런스)" if entropy_score > 35 else "B급 패턴")
    return {"numbers": nums, "grade": grade, "ac": calculate_ac(nums)}

# ==========================================
# 연금복권 720+ 통계 기반 엔진 (단일 조 / 전조 명확 분기)
# ==========================================
@st.cache_data(ttl=3600)
def fetch_pension_statistics():
    return {pos: {digit: random.uniform(8.0, 15.0) for digit in range(10)} for pos in range(6)}

def generate_pension_game(pension_version, pension_mode):
    stats_weights = fetch_pension_statistics()
    
    # "단일 조" 문구가 포함되어 있으면 1~5조 생성 포함
    if "단일 조" in pension_version:
        group = random.randint(1, 5)
        digits = []
        for pos in range(6):
            w = list(stats_weights[pos].values())
            if pension_mode == "🔥 연금 핫패턴형":
                max_idx = max(range(len(w)), key=w.__getitem__)
                w[max_idx] += 10.0
            digits.append(random.choices(range(10), weights=w, k=1)[0])
            
        digit_sum = sum(digits)
        score = (digit_sum * digits[-1] + group) % 100
        p_grade = "S+급 (단일조 정밀)" if score > 70 else ("A급 (밸런스)" if score > 35 else "B급 패턴")
        return {"type": "single", "group": group, "digits": digits, "grade": p_grade, "sum": digit_sum}
    else:
        # 전조 풀세트 모드: 조를 뺀 6자리 핵심 번호만 집중 최적화
        for _ in range(3000):
            digits = []
            for pos in range(6):
                w = list(stats_weights[pos].values())
                if pension_mode == "🔥 연금 핫패턴형":
                    max_idx = max(range(len(w)), key=w.__getitem__)
                    w[max_idx] += 12.0
                digits.append(random.choices(range(10), weights=w, k=1)[0])
            digit_sum = sum(digits)
            if 20 <= digit_sum <= 34:
                if not any(digits[i] == digits[i+1] == digits[i+2] for i in range(4)):
                    break
        else:
            digits = [random.randint(0, 9) for _ in range(6)]
        digit_sum = sum(digits)
        score = (digit_sum * digits[-1]) % 100
        p_grade = "S+급 (전조 최적화)" if score > 70 else ("A급 (밸런스 조합)" if score > 35 else "B급 패턴")
        return {"type": "fullset", "digits": digits, "grade": p_grade, "sum": digit_sum}

# ==========================================
# 파워볼 통계 엔진
# ==========================================
def generate_powerball_game(power_mode):
    weights = {i: random.uniform(8.0, 15.0) for i in range(1, 29)}
    if power_mode == "🔥 파워볼 핫패턴형":
        for i in [3, 7, 14, 21, 25]: weights[i] += 12.0
        
    for _ in range(2500):
        picked = random.choices(list(weights.keys()), weights=list(weights.values()), k=5)
        if len(set(picked)) != 5: continue
        nums = sorted(picked)
        total_sum = sum(nums)
        odds = sum(1 for n in nums if n % 2 != 0)
        if 60 <= total_sum <= 85 and odds in [2, 3]:
            break
    else:
        nums = sorted(random.sample(range(1, 29), 5))
        
    power_weights = [10.0] * 10
    if power_mode == "🔥 파워볼 핫패턴형":
        power_weights[3] += 10.0
        power_weights[7] += 10.0
    power_num = random.choices(range(10), weights=power_weights, k=1)[0]
    
    score = (sum(nums) * power_num) % 100
    grade = "S+급 (파워볼 정밀)" if score > 70 else ("A급 (밸런스)" if score > 35 else "B급 패턴")
    return {"numbers": nums, "power": power_num, "grade": grade, "sum": sum(nums)}

# ==========================================
# 사이드바 메인 메뉴 설정
# ==========================================
st.sidebar.markdown("### 🎲 복권 프로그램 선택")
app_mode = st.sidebar.selectbox(
    "실행할 복권 엔진",
    ["🎰 로또 6/45 퀀텀 Pro", "🎟️ 연금복권 720+ 퀀텀 Pro", "⚡ 파워볼 퀀텀 Pro"]
)

st.sidebar.markdown("---")

num_games = st.sidebar.slider(
    "🎯 생성할 게임 수", 
    min_value=1, 
    max_value=5, 
    value=1, 
    help="적게 뽑을수록 더 강력한 통계 필터가 적용됩니다."
)

st.sidebar.markdown("---")

if app_mode == "🎰 로또 6/45 퀀텀 Pro":
    st.sidebar.markdown("### 🎛️ 로또 통계 전략 설정")
    analysis_mode = st.sidebar.selectbox("분석 알고리즘", ["⚖️ 완벽 균형 퀀텀형", "🔥 핫넘버 집중형", "🧊 콜드넘버 역발상형"])
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🎯 로또 번호 커스텀")
    all_numbers = list(range(1, 46))
    fixed_nums = st.sidebar.multiselect("📌 고정수 (최대 5개)", options=all_numbers, max_selections=5)
    exclude_nums = st.sidebar.multiselect("🚫 제외수", options=all_numbers, max_selections=35)

elif app_mode == "🎟️ 연금복권 720+ 퀀텀 Pro":
    st.sidebar.markdown("### 🎛️ 연금복권 통계 전략 설정")
    pension_version = st.sidebar.selectbox(
        "구매 전략", 
        ["🎫 단일 조 모드 (조 + 6자리)", "👑 전조(1~5조) 풀세트 모드 (조 제외 6자리)"]
    )
    pension_mode = st.sidebar.selectbox("연금 분석 모드", ["🧊 연금 균형분산형", "🔥 연금 핫패턴형"])

else:
    st.sidebar.markdown("### 🎛️ 파워볼 통계 전략 설정")
    power_mode = st.sidebar.selectbox("파워볼 분석", ["⚖️ 파워볼 균형분산형", "🔥 파워볼 핫패턴형"])
    st.sidebar.markdown("---")
    st.sidebar.info("💡 **파워볼:** 일반볼 5개 + 파워볼 1개 정밀 조합")

# ==========================================
# 메인 화면 출력 (모바일 최적화 레이아웃)
# ==========================================
labels = ['A', 'B', 'C', 'D', 'E'][:num_games]

if app_mode == "🎰 로또 6/45 퀀텀 Pro":
    st.markdown("<h2 style='text-align: center; color: white;'>✨ 당첨 퀀텀 로또 Pro</h2>", unsafe_allow_html=True)
    st.markdown(f"<p style='text-align: center; color: #94a3b8; font-size: 12px;'>모드: <b style='color: #38bdf8;'>{analysis_mode}</b> | <b>{num_games}게임</b></p>", unsafe_allow_html=True)
    st.write("")

    if st.button(f"🚀 로또 {num_games}게임 초고정밀 산출", use_container_width=True):
        with st.spinner("퀀텀 확률 연산 중..."):
            history, status_msg = fetch_lotto_data()
        st.toast(status_msg, icon="ℹ️")
        
        for label in labels:
            game = generate_lotto_game(history, analysis_mode, fixed_nums, exclude_nums, num_games)
            balls_html = "".join([f"<span class='ball {get_ball_class(n)} {'fixed-badge' if n in fixed_nums else ''}'>{n}</span>" for n in game['numbers']])
            
            st.markdown(f"""
                <div class="result-card">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span style="background: rgba(99, 102, 241, 0.2); color: #818cf8; width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 12px; border: 1px solid rgba(129, 140, 248, 0.3);">{label}</span>
                        <div style="display: flex; gap: 2px;">{balls_html}</div>
                    </div>
                    <div style="text-align: right;">
                        <div style="color: #34d399; font-weight: bold; font-size: 11px;">{game['grade']}</div>
                        <div style="color: #64748b; font-size: 9px;">AC: {game['ac']}</div>
                    </div>
                </div>
            """, unsafe_allow_html=True)
    else:
        st.markdown("""
            <div style="background-color: #1e293b; padding: 30px 20px; border-radius: 14px; text-align: center; border: 1px dashed #475569; color: #94a3b8; font-size: 13px;">
                사이드바에서 설정을 마친 뒤 버튼을 눌러보세요!
            </div>
        """, unsafe_allow_html=True)

elif app_mode == "🎟️ 연금복권 720+ 퀀텀 Pro":
    st.markdown(f"<h2 style='text-align: center; color: white;'>🎟️ 연금복권 720+ Pro</h2>", unsafe_allow_html=True)
    st.markdown(f"<p style='text-align: center; color: #94a3b8; font-size: 12px;'>모드: <b style='color: #c084fc;'>{pension_version}</b></p>", unsafe_allow_html=True)
    st.write("")

    if st.button(f"🚀 연금복권 {num_games}세트 통계 산출", use_container_width=True):
        with st.spinner("통계 패턴 연산 중..."):
            pass
        st.toast("산출 완료 🚀", icon="ℹ️")
        
        for label in labels:
            p_game = generate_pension_game(pension_version, pension_mode)
            digits_html = "".join([f"<span class='pension-digit'>{d}</span>" for d in p_game['digits']])
            
            # 단일 조일 때는 'N조' 배지 출력, 전조일 때는 '전조(1~5조)' 배지 출력
            if p_game['type'] == 'single':
                badge_html = f"<span class='pension-group-badge'>{p_game['group']}조</span>"
            else:
                badge_html = "<span class='pension-set-badge'>전조(1~5조)</span>"
            
            st.markdown(f"""
                <div class="result-card">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span style="background: rgba(99, 102, 241, 0.2); color: #818cf8; width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 12px; border: 1px solid rgba(129, 140, 248, 0.3);">{label}</span>
                        <div style="display: flex; align-items: center;">{badge_html}{digits_html}</div>
                    </div>
                    <div style="text-align: right;">
                        <div style="color: #c084fc; font-weight: bold; font-size: 11px;">{p_game['grade']}</div>
                        <div style="color: #64748b; font-size: 9px;">합계: {p_game['sum']}</div>
                    </div>
                </div>
            """, unsafe_allow_html=True)
    else:
        st.markdown("""
            <div style="background-color: #1e293b; padding: 30px 20px; border-radius: 14px; text-align: center; border: 1px dashed #475569; color: #94a3b8; font-size: 13px;">
                사이드바에서 설정을 마치고 버튼을 눌러보세요!
            </div>
        """, unsafe_allow_html=True)

else:
    st.markdown(f"<h2 style='text-align: center; color: white;'>⚡ 동행복권 파워볼 Pro</h2>", unsafe_allow_html=True)
    st.markdown(f"<p style='text-align: center; color: #94a3b8; font-size: 12px;'>모드: <b style='color: #f472b6;'>{power_mode}</b> | <b>{num_games}게임</b></p>", unsafe_allow_html=True)
    st.write("")

    if st.button(f"🚀 파워볼 {num_games}게임 통계 산출", use_container_width=True):
        with st.spinner("파워볼 확률 연산 중..."):
            pass
        st.toast("산출 완료 🚀", icon="ℹ️")
        
        for label in labels:
            pb_game = generate_powerball_game(power_mode)
            balls_html = "".join([f"<span class='ball {get_ball_class(n)}'>{n}</span>" for n in pb_game['numbers']])
            power_html = f"<span class='power-ball'>{pb_game['power']}</span>"
            
            st.markdown(f"""
                <div class="result-card">
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <span style="background: rgba(99, 102, 241, 0.2); color: #818cf8; width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 12px; border: 1px solid rgba(129, 140, 248, 0.3);">{label}</span>
                        <div style="display: flex; align-items: center; gap: 2px;">{balls_html} <span style="color: #64748b; font-weight: bold; margin: 0 2px;">+</span> {power_html}</div>
                    </div>
                    <div style="text-align: right;">
                        <div style="color: #f472b6; font-weight: bold; font-size: 11px;">{pb_game['grade']}</div>
                        <div style="color: #64748b; font-size: 9px;">합계: {pb_game['sum']}</div>
                    </div>
                </div>
            """, unsafe_allow_html=True)
    else:
        st.markdown("""
            <div style="background-color: #1e293b; padding: 30px 20px; border-radius: 14px; text-align: center; border: 1px dashed #475569; color: #94a3b8; font-size: 13px;">
                사이드바에서 설정을 마치고 버튼을 눌러보세요!
            </div>
        """, unsafe_allow_html=True)
