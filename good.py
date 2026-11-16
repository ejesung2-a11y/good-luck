import streamlit as st
import requests
import random
from datetime import datetime

# 페이지 설정
st.set_page_config(
    page_title="당첨 퀀텀 로또 제너레이터 Pro",
    page_icon="✨",
    layout="centered"
)

# 다크 모드 스타일 및 공 색상 디자인 적용
st.markdown("""
    <style>
    .stApp {
        background-color: #0f172a;
        color: #f8fafc;
    }
    .ball {
        display: inline-flex;
        width: 38px;
        height: 38px;
        border-radius: 50%;
        align-items: center;
        justify-content: center;
        font-weight: bold;
        color: white;
        margin: 0 3px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        font-size: 14px;
    }
    .ball-1 { background-color: #f59e0b; } /* 1~10: 노란/주황 */
    .ball-2 { background-color: #0284c7; } /* 11~20: 파랑 */
    .ball-3 { background-color: #e11d48; } /* 21~30: 빨강 */
    .ball-4 { background-color: #475569; } /* 31~40: 회색 */
    .ball-5 { background-color: #10b981; } /* 41~45: 초록 */
    .fixed-badge {
        border: 2px solid #fde047;
        box-shadow: 0 0 8px rgba(253, 224, 71, 0.6);
    }
    </style>
""", unsafe_allow_html=True)

def get_ball_class(num):
    if num <= 10: return "ball-1"
    if num <= 20: return "ball-2"
    if num <= 30: return "ball-3"
    if num <= 40: return "ball-4"
    return "ball-5"

# 동행복권 실시간 데이터 가져오기 (캐싱 적용)
@st.cache_data(ttl=3600)
def fetch_lotto_data():
    try:
        ref_date = datetime(2002, 12, 7)
        days_diff = (datetime.now() - ref_date).days
        latest_round = (days_diff // 7) + 1
        
        history = []
        start_round = max(1, latest_round - 50)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        
        for drw in range(latest_round, start_round - 1, -1):
            url = f"https://www.dhlottery.co.kr/common.do?method=getLottoNumber&drwNo={drw}"
            res = requests.get(url, headers=headers, timeout=3).json()
            if res.get("returnValue") == "success":
                numbers = [
                    res["drwtNo1"], res["drwtNo2"], res["drwtNo3"],
                    res["drwtNo4"], res["drwtNo5"], res["drwtNo6"]
                ]
                history.append({
                    "round": drw,
                    "numbers": numbers,
                    "bonus": res["bnusNo"]
                })
        return history, latest_round, "실시간 동행복권 데이터 연동 완료 🚀"
    except Exception as e:
        return [], 0, "오프라인 모드 (기본 통계 엔진 가동 중 ⚠️)"

# AC값(복잡도) 계산 함수
def calculate_ac(nums):
    diffs = set()
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            diffs.add(abs(nums[i] - nums[j]))
    return len(diffs) - (len(nums) - 1)

# 구간 분산 체크 함수
def count_zones(nums):
    zones = set()
    for n in nums:
        zones.add((n - 1) // 10)
    return len(zones)

# 고정수/제외수가 반영된 고도화 확률 통합 알고리즘
def generate_single_game(history, mode, fixed_nums, exclude_nums):
    # 고정수와 제외수 충돌 시 고정수 우선
    clean_exclude = set(exclude_nums) - set(fixed_nums)
    
    # 선택 가능한 후보 풀 구성 (고정수 및 제외수 배제)
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
            else: # ⚖️ 완벽 균형 퀀텀형
                if i in latest_numbers: weights[i] += 12.0
                weights[i] += (freq[i] * 2.0)

    needed_count = 6 - len(fixed_nums)
    
    # 후보군이 부족할 경우 예외 처리
    if len(available_nums) < needed_count:
        nums = sorted(list(fixed_nums) + random.sample([i for i in range(1, 46) if i not in fixed_nums], needed_count))
        return {"numbers": nums, "grade": "커스텀 강제 조합", "ac": calculate_ac(nums)}

    # 전문 필터 루프
    max_attempts = 2500
    for _ in range(max_attempts):
        picked = random.choices(list(weights.keys()), weights=list(weights.values()), k=needed_count)
        if len(set(picked)) != needed_count:
            continue
            
        nums = sorted(list(fixed_nums) + picked)
        
        total_sum = sum(nums)
        odds = sum(1 for n in nums if n % 2 != 0)
        ac_val = calculate_ac(nums)
        zone_count = count_zones(nums)
        
        # 고정수가 많을수록 필터 조건을 유연하게 적용
        if len(fixed_nums) >= 3:
            if ac_val >= 4:
                break
        else:
            if 100 <= total_sum <= 170 and 2 <= odds <= 4 and ac_val >= 5 and zone_count >= 3:
                has_triple = any(nums[i] == nums[i+1]-1 == nums[i+2]-2 for i in range(4))
                if not has_triple:
                    break
    else:
        # 조건 만족 실패 시 가중치 기반 최선 조합 산출
        while True:
            picked = random.choices(list(weights.keys()), weights=list(weights.values()), k=needed_count)
            if len(set(picked)) == needed_count:
                nums = sorted(list(fixed_nums) + picked)
                break

    # 엔트로피 및 등급 산출
    entropy_score = (sum(nums) * nums[-1] + calculate_ac(nums)) % 100
    if entropy_score > 75:
        grade = "S급 (퀀텀 하이퍼 기운)"
    elif entropy_score > 40:
        grade = "A급 (스마트 밸런스형)"
    else:
        grade = "B급 (변칙 일탈형)"

    return {"numbers": nums, "grade": grade, "ac": calculate_ac(nums)}

# ==========================================
# 사이드바 설정 (전략 모드 + 고정수/제외수)
# ==========================================
st.sidebar.markdown("### 🎛️ 통계 엔진 전략 설정")
analysis_mode = st.sidebar.selectbox(
    "분석 알고리즘 모드",
    ["⚖️ 완벽 균형 퀀텀형", "🔥 핫넘버 집중형", "🧊 콜드넘버 역발상형"]
)

st.sidebar.markdown("---")
st.sidebar.markdown("### 🎯 나만의 번호 커스텀")

all_numbers = list(range(1, 46))

fixed_nums = st.sidebar.multiselect(
    "📌 고정수 (무조건 포함, 최대 5개)",
    options=all_numbers,
    max_selections=5,
    placeholder="행운의 숫자를 선택하세요"
)

exclude_nums = st.sidebar.multiselect(
    "🚫 제외수 (절대 배제할 번호)",
    options=all_numbers,
    max_selections=35,
    placeholder="제외할 숫자를 선택하세요"
)

# 중복 선택 알림
overlap = set(fixed_nums) & set(exclude_nums)
if overlap:
    st.sidebar.warning(f"⚠️ {sorted(list(overlap))}번은 고정수와 제외수에 중복 선택되어 '고정수'로 우선 적용됩니다.")

st.sidebar.markdown("---")
st.sidebar.info("💡 **AC값 필터 및 구간 분산**이 적용되며, 선택한 고정수는 노란 테두리로 강조 표시됩니다.")

# ==========================================
# 메인 UI 구성
# ==========================================
st.markdown("<h1 style='text-align: center; color: white;'>✨ 당첨 퀀텀 로또 생성기 Pro</h1>", unsafe_allow_html=True)
st.markdown(f"<p style='text-align: center; color: #94a3b8; font-size: 13px;'>가동 엔진: <b style='color: #38bdf8;'>{analysis_mode}</b> | 고정수: <b>{len(fixed_nums)}개</b> | 제외수: <b>{len(exclude_nums)}개</b></p>", unsafe_allow_html=True)
st.write("")

if st.button("🚀 이번 주 행운의 5게임(A~E) 고도화 산출하기", use_container_width=True):
    with st.spinner("역대 데이터 동기화, 커스텀 필터 및 퀀텀 확률 연산 중..."):
        history, latest_round, status_msg = fetch_lotto_data()
    
    st.toast(status_msg, icon="ℹ️")
    
    labels = ['A', 'B', 'C', 'D', 'E']
    for label in labels:
        game = generate_single_game(history, analysis_mode, fixed_nums, exclude_nums)
        
        balls_html = ""
        for n in game['numbers']:
            # 고정수로 지정된 번호는 노란색 빛나는 테두리 효과 추가
            extra_class = "fixed-badge" if n in fixed_nums else ""
            balls_html += f"<span class='ball {get_ball_class(n)} {extra_class}'>{n}</span>"
        
        st.markdown(f"""
            <div style="background-color: #1e293b; padding: 14px 18px; border-radius: 12px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; border: 1px solid #334155;">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <span style="background: rgba(99, 102, 241, 0.2); color: #818cf8; width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 13px; border: 1px solid rgba(129, 140, 248, 0.3);">{label}</span>
                    <div style="display: flex; gap: 4px;">{balls_html}</div>
                </div>
                <div style="text-align: right;">
                    <div style="color: #34d399; font-weight: bold; font-size: 12px;">{game['grade']}</div>
                    <div style="color: #64748b; font-size: 10px;">AC 복잡도: {game['ac']}</div>
                </div>
            </div>
        """, unsafe_allow_html=True)
else:
    st.markdown("""
        <div style="background-color: #1e293b; padding: 40px; border-radius: 16px; text-align: center; border: 1px dashed #475569; color: #94a3b8;">
            좌측 사이드바에서 전략 모드와 <b>고정수·제외수</b>를 설정한 뒤 버튼을 눌러보세요!
        </div>
    """, unsafe_allow_html=True)