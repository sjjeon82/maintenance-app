import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, timezone
import time
import streamlit.components.v1 as components
from streamlit_gsheets import GSheetsConnection

# 💡 시간대: 한국 표준시(KST, UTC+9) 고정 오프셋. (한국은 서머타임 없음 → 항상 +9)
#    서버가 UTC여도 등록 시각/조회 날짜가 한국 시간 기준이 되도록 강제.
KST = timezone(timedelta(hours=9))

st.set_page_config(page_title="모바일 정비예약", layout="wide")

# 1. 구글 시트 DB 연결
conn = st.connection("gsheets", type=GSheetsConnection)

def get_display_slots():
    morning = [f"{h:02d}:{m:02d}" for h in range(8, 12) for m in (0, 30)]
    afternoon = [f"{h:02d}:{m:02d}" for h in range(13, 17) for m in (0, 30)]
    return morning + afternoon

TASK_CATEGORIES = ["차량정비", "마이카", "오일교환", "요소수보충", "정비점검", "타이어점검", "경정비", "기타"]

# 💡 정원제: 유형별 30분당 최대 대수(limit) / 선택 가능한 소요시간(분) / 정원 풀(pool)
#   pool "general": 잠깐 들르는 차량 → SLOT_TOTAL_LIMIT(총 정원)에 포함
#   pool "dock"   : 차량정비(장시간) → 정비도크 수(limit)로만 별도 관리
TASK_CONFIG = {
    "차량정비": {"color": "#6B46C1", "icon": "🛠️", "limit": 4, "durations": list(range(60, 421, 30)), "pool": "dock"},
    "마이카": {"color": "#D53F8C", "icon": "🚗", "limit": 2, "durations": [30, 60], "pool": "general"},
    "오일교환": {"color": "#FFA500", "icon": "🛢️", "limit": 2, "durations": [30], "pool": "general"},
    "요소수보충": {"color": "#1E90FF", "icon": "💦", "limit": 2, "durations": [30], "pool": "general"},
    "정비점검": {"color": "#FF4B4B", "icon": "🔧", "limit": 1, "durations": [30], "pool": "general"},
    "타이어점검": {"color": "#4A5568", "icon": "🛞", "limit": 1, "durations": [30], "pool": "general"},
    "경정비": {"color": "#28A745", "icon": "📦", "limit": 2, "durations": [30], "pool": "general"},
    "기타": {"color": "#718096", "icon": "📌", "limit": 1, "durations": [30, 60], "pool": "general"},
}

# 상세내용 입력란을 보여줄 유형 / 그중 필수 입력 유형
DETAIL_TYPES = ["정비점검", "차량정비", "경정비", "기타"]
DETAIL_REQUIRED = ["정비점검", "차량정비", "경정비", "기타"]

# 💡 정원제: 한 슬롯(30분)에 동시 입고 가능한 총 최대 대수 (general 풀에만 적용)
SLOT_TOTAL_LIMIT = 4

# 부품대기 상태: 정비고에는 있지만 도크/정비사를 점유하지 않음 → 정원 계산에서 제외
STATUS_WAITING = "부품대기"

slots = get_display_slots()


def duration_label(minutes):
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}시간 {m}분"
    return f"{h}시간" if h else f"{m}분"


def slot_range(start_time, minutes):
    """시작 슬롯부터 소요시간만큼 점유하는 슬롯 목록 (점심시간 자동 건너뜀). 17:00 초과 시 None."""
    if start_time not in slots:
        return None
    idx = slots.index(start_time)
    n = int(minutes) // 30
    if idx + n > len(slots):
        return None
    return slots[idx:idx + n]


def end_time_of(occupied):
    """점유 슬롯 목록의 마지막 슬롯 + 30분 = 출고 시간."""
    return (datetime.strptime(occupied[-1], "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")


def first_slot_of(start_time, end_time):
    """예약이 점유하는 첫 슬롯 (목록에서 이 슬롯에만 표시). 없으면 None."""
    return next((s for s in slots if start_time <= s < end_time), None)


def booking_minutes(start_time, end_time):
    """기존 예약의 입고~출고 사이 점유 슬롯 수로 소요시간(분) 역산."""
    return 30 * max(1, sum(1 for s in slots if start_time <= s < end_time))


# ------------------------------------------------------------------
# 🔧 공통 유틸: 데이터 정규화 / 슬롯 점유 카운트 / 사전·당일 판별
# ------------------------------------------------------------------
def normalize_df(df):
    """id 없는 행 제거 후 date/start_time/end_time을 표준 문자열 포맷으로 정규화."""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=['id']).copy()
    if df.empty:
        return df
    df['date'] = pd.to_datetime(df['date'], format='mixed', errors='coerce').dt.strftime('%Y-%m-%d')
    df['start_time'] = pd.to_datetime(df['start_time'].astype(str), format='mixed', errors='coerce').dt.strftime('%H:%M')
    df['end_time'] = pd.to_datetime(df['end_time'].astype(str), format='mixed', errors='coerce').dt.strftime('%H:%M')
    if 'status' not in df.columns:
        df['status'] = ""
    df['status'] = df['status'].fillna("").astype(str)
    return df


def count_slot(df_norm, slot):
    """
    해당 슬롯을 점유하는 '활성' 예약(부품대기 제외)의 (유형별 카운트, general 풀 총 카운트) 반환.
    차량정비(dock 풀)는 총 정원에 포함하지 않음.
    """
    if df_norm is None or df_norm.empty:
        return {}, 0
    occ = df_norm[(df_norm['start_time'] <= slot) & (df_norm['end_time'] > slot)]
    if 'status' in occ.columns:
        occ = occ[occ['status'] != STATUS_WAITING]
    type_counts = occ['task_type'].value_counts().to_dict()
    general_total = sum(
        c for t, c in type_counts.items()
        if TASK_CONFIG.get(t, {}).get("pool", "general") == "general"
    )
    return type_counts, general_total


def slot_has_room(type_counts, general_total, task_type):
    cfg = TASK_CONFIG[task_type]
    if type_counts.get(task_type, 0) >= cfg["limit"]:
        return False
    if cfg["pool"] == "general" and general_total >= SLOT_TOTAL_LIMIT:
        return False
    return True


def check_capacity(df_norm, start_time, task_type, minutes, exclude_id=None):
    """
    저장 직전 정원 재검증 — 예약이 걸치는 '모든' 슬롯을 검사. 통과하면 (True, ""), 아니면 (False, 사유).
    exclude_id: 수정 시 자기 자신을 카운트에서 제외하기 위한 예약 id.
    """
    occupied = slot_range(start_time, minutes)
    if not occupied:
        return False, f"운영시간(17:00)을 넘깁니다. 입고 시간을 앞당기거나 소요시간을 줄여주세요."

    df_check = df_norm
    if exclude_id is not None and df_check is not None and not df_check.empty:
        df_check = df_check[df_check['id'] != exclude_id]

    cfg = TASK_CONFIG[task_type]
    for s in occupied:
        type_counts, general_total = count_slot(df_check, s)
        if cfg["pool"] == "general" and general_total >= SLOT_TOTAL_LIMIT:
            return False, f"{s} 시간대는 총 정원({SLOT_TOTAL_LIMIT}대)이 모두 찼습니다."
        if type_counts.get(task_type, 0) >= cfg["limit"]:
            unit = "정비도크" if cfg["pool"] == "dock" else "정원"
            josa = "가" if cfg["pool"] == "dock" else "이"
            return False, f"{s} 시간대 '{task_type}' {unit}({cfg['limit']}대){josa} 찼습니다. 다른 시간을 선택하세요."

    return True, ""


def classify_booking(created_at_val, service_date_str):
    """
    등록일(created_at) vs 예약일(date) 비교로 사전/당일 판별.
    반환: (라벨, 뱃지아이콘)
    """
    if created_at_val is None or pd.isna(created_at_val) or str(created_at_val).strip() in ("", "nan", "NaT"):
        return "미분류", ""
    cdate = pd.to_datetime(created_at_val, format='mixed', errors='coerce')
    sdate = pd.to_datetime(service_date_str, format='mixed', errors='coerce')
    if pd.isna(cdate) or pd.isna(sdate):
        return "미분류", ""
    if cdate.date() < sdate.date():
        return "사전", "📅"
    return "당일", "⚡"


# --- 🚀 신규 예약 등록 모달창 ---
@st.dialog("➕ 신규 예약 등록")
def create_reservation_modal(selected_date, start_time, df_res):
    st.info(f"선택 시간: **{selected_date} / {start_time}**")

    # 💡 정원제: 이 슬롯의 현재 점유 상황 계산
    type_counts, general_total = count_slot(df_res, start_time)
    remaining_total = SLOT_TOTAL_LIMIT - general_total

    # 아직 자리가 남은 유형만 선택지로 노출
    available_types = [t for t in TASK_CATEGORIES if slot_has_room(type_counts, general_total, t)]

    if not available_types:
        st.error("이 시간대는 예약이 마감되었습니다.")
        return

    # 잔여 현황 안내
    remain_txt = " / ".join(
        [f"{TASK_CONFIG[t]['icon']}{t} {TASK_CONFIG[t]['limit'] - type_counts.get(t, 0)}"
         for t in available_types]
    )
    st.caption(f"🅿️ 이 시간대 잔여 — 일반 총 **{max(remaining_total, 0)}대** ｜ {remain_txt}")

    v_no = st.text_input("차량 번호")
    m_name = st.text_input("운전자")
    task_type = st.selectbox("정비 유형", available_types)

    durations = TASK_CONFIG[task_type]["durations"]
    if len(durations) > 1:
        minutes = st.selectbox("⏳ 예상 소요시간", durations, format_func=duration_label)
    else:
        minutes = durations[0]

    details = ""
    if task_type in DETAIL_TYPES:
        req = " (필수 기입)" if task_type in DETAIL_REQUIRED else ""
        details = st.text_area(f"📝 상세내용{req}")

    occupied = slot_range(start_time, minutes)
    if occupied:
        e_time = end_time_of(occupied)
        st.caption(f"⏱️ 출고 예정 **{e_time}** ({duration_label(minutes)}, 점심시간 제외)")
    else:
        e_time = None
        st.warning("운영시간(17:00)을 넘깁니다. 소요시간을 줄이거나 더 이른 시간을 선택하세요.")

    if st.button("예약 저장", type="primary", use_container_width=True):
        if not v_no or not m_name:
            st.error("차량 번호와 운전자를 입력해주세요.")
        elif task_type in DETAIL_REQUIRED and not details.strip():
            st.warning("상세내용을 기입해주세요.")
        elif not e_time:
            st.error("운영시간을 넘기는 예약은 저장할 수 없습니다.")
        else:
            df_raw = conn.read(worksheet="Sheet1", ttl=0)
            df = df_raw.dropna(subset=['id']) if df_raw is not None else pd.DataFrame()

            # 💡 정원제: 저장 직전 최신 데이터로 정원 재검증 (동시 저장 방어)
            df_norm_all = normalize_df(df_raw)
            df_norm_day = df_norm_all[df_norm_all['date'] == str(selected_date)] if not df_norm_all.empty else pd.DataFrame()
            ok, reason = check_capacity(df_norm_day, start_time, task_type, minutes)
            if not ok:
                st.error(f"⛔ {reason}")
                st.stop()

            new_id = int(df['id'].max() + 1) if not df.empty and pd.notna(df['id'].max()) else 1

            # 💡 사전/당일 구분용: 등록 시각 기록 (KST 기준)
            created_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

            # 💡 핵심 픽스: 구글 시트의 숫자 강제 변환을 막기 위해 앞에 싱글 쿼테이션(') 주입
            new_row = pd.DataFrame([{
                "id": new_id, "date": str(selected_date), "start_time": start_time,
                "end_time": e_time, "vehicle_no": f"'{v_no}", "manager": m_name,
                "task_type": task_type, "details": details, "created_at": created_at,
                "status": ""
            }])

            updated_df = pd.concat([df, new_row], ignore_index=True)
            conn.update(worksheet="Sheet1", data=updated_df)

            st.cache_data.clear()
            time.sleep(1.5)
            st.rerun()


# --- 🚀 예약 조회 / 수정 / 삭제 통합 모달창 ---
@st.dialog("📋 예약 상세 및 관리")
def reservation_modal(res_id, v_no, manager, t_type, s_time, e_time, details, status, selected_date, booking_label):
    # 💡 사전/당일 라벨 표시
    st.caption(f"구분: **{booking_label} 예약**")

    new_v_no = st.text_input("차량 번호", value=v_no)
    new_manager = st.text_input("운전자", value=manager)

    type_idx = TASK_CATEGORIES.index(t_type) if t_type in TASK_CATEGORIES else 0
    new_task_type = st.selectbox("정비 유형", TASK_CATEGORIES, index=type_idx)

    new_details = details
    if new_task_type in DETAIL_TYPES:
        new_details = st.text_area("📝 상세내용", value=details if details else "")

    s_idx = slots.index(s_time) if s_time in slots else 0
    new_s_time = st.selectbox("입고 시간", slots, index=s_idx)

    # 💡 소요시간 변경 = 조기 출고(줄이기) / 연장(늘리기)
    durations = TASK_CONFIG[new_task_type]["durations"]
    cur_minutes = booking_minutes(s_time, e_time) if e_time else durations[0]
    if len(durations) > 1:
        d_idx = durations.index(cur_minutes) if cur_minutes in durations else 0
        new_minutes = st.selectbox("⏳ 소요시간 (조기출고·연장 시 변경)", durations, index=d_idx, format_func=duration_label)
    else:
        new_minutes = durations[0]

    new_status = status
    if TASK_CONFIG[new_task_type]["pool"] == "dock":
        is_waiting = st.checkbox("⏸️ 부품대기 (도크 정원에서 제외, 정비고 내 대기)", value=(status == STATUS_WAITING))
        new_status = STATUS_WAITING if is_waiting else ""
    else:
        new_status = ""

    occupied = slot_range(new_s_time, new_minutes)
    if occupied:
        new_e_time = end_time_of(occupied)
        st.caption(f"⏱️ 출고 예정 **{new_e_time}** ({duration_label(new_minutes)}, 점심시간 제외)")
    else:
        new_e_time = None
        st.warning("운영시간(17:00)을 넘깁니다. 소요시간을 줄이거나 입고 시간을 앞당기세요.")

    st.divider()
    col_update, col_delete = st.columns(2)
    with col_update:
        if st.button("💾 저장", type="primary", use_container_width=True):
            if not new_v_no or not new_manager:
                st.error("차량 번호와 운전자를 입력해주세요.")
            elif not new_e_time:
                st.error("운영시간을 넘기는 예약은 저장할 수 없습니다.")
            else:
                df_raw = conn.read(worksheet="Sheet1", ttl=0)
                df = df_raw.dropna(subset=['id']).copy()
                if 'status' not in df.columns:
                    df['status'] = ""

                # 💡 정원제: 수정 시에도 재검증하되, 자기 자신(res_id)은 카운트에서 제외
                #    부품대기 차량은 정원을 점유하지 않으므로 검증 생략
                if new_status != STATUS_WAITING:
                    df_norm_all = normalize_df(df_raw)
                    df_norm_day = df_norm_all[df_norm_all['date'] == str(selected_date)] if not df_norm_all.empty else pd.DataFrame()
                    ok, reason = check_capacity(df_norm_day, new_s_time, new_task_type, new_minutes, exclude_id=res_id)
                    if not ok:
                        st.error(f"⛔ {reason}")
                        st.stop()

                idx = df.index[df['id'] == res_id].tolist()

                if idx:
                    df['status'] = df['status'].fillna("").astype(str)
                    df.at[idx[0], 'status'] = new_status
                    df.at[idx[0], 'start_time'] = new_s_time
                    df.at[idx[0], 'end_time'] = new_e_time
                    # 💡 핵심 픽스: 수정 시에도 싱글 쿼테이션(') 주입
                    df.at[idx[0], 'vehicle_no'] = f"'{new_v_no}"
                    df.at[idx[0], 'manager'] = new_manager
                    df.at[idx[0], 'task_type'] = new_task_type
                    df.at[idx[0], 'details'] = new_details
                    # created_at은 등록 시점 값 유지를 위해 건드리지 않음
                    conn.update(worksheet="Sheet1", data=df)

                st.cache_data.clear()
                time.sleep(1.5)
                st.rerun()

    with col_delete:
        if st.button("🗑️ 삭제", type="secondary", use_container_width=True):
            df = conn.read(worksheet="Sheet1", ttl=0)
            df = df.dropna(subset=['id'])

            updated_df = df[df['id'] != res_id]
            conn.update(worksheet="Sheet1", data=updated_df)

            st.cache_data.clear()
            time.sleep(1.5)
            st.rerun()


# --- 📱 모바일 절대 방어 CSS ---
st.markdown("""
<style>
    .stApp { background-color: #f7f9fc; }
    /* 상단 여백 3.75rem = Streamlit 상단 메뉴바 높이 → 모바일에서 제목이 메뉴바에 가려지지 않도록 */
    .block-container { padding: 3.75rem 0.5rem 2rem 0.5rem !important; max-width: 100% !important; }
    .main-title { font-size: 20px; font-weight: 800; color: #1a202c; text-align: center; margin-bottom: 10px; }
    .legend-box { background: #ffffff; padding: 8px; border-radius: 8px; font-size: 11px; color: #4a5568; display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; box-shadow: 0 1px 2px rgba(0,0,0,0.05); margin-bottom: 15px; border: 1px solid #e2e8f0; }

    @media (max-width: 640px) {
        div[data-testid="stHorizontalBlock"] { display: grid !important; grid-template-columns: 50px 1fr 45px !important; gap: 5px !important; width: 100% !important; margin-bottom: 6px !important; }
        div[data-testid="column"] { width: 100% !important; min-width: 0 !important; }
        div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] { display: flex !important; flex-direction: row !important; gap: 10px !important; }
        div[data-testid="stDialog"] div[data-testid="column"] { width: 50% !important; flex: 1 1 50% !important; }
    }

    div[data-testid="stButton"] button { border-radius: 6px !important; min-height: 40px !important; padding: 0 !important; width: 100% !important; overflow: hidden !important; }
    div[data-testid="stButton"] button p { font-size: 13px !important; font-weight: 600 !important; margin: 0 !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; }
    button[kind="secondary"] { background-color: white !important; border: 1px solid #e2e8f0 !important; }
    button[kind="secondary"] p { color: #1a202c !important; }
    button[kind="primary"] { border: none !important; }
    button[kind="primary"] p { color: white !important; }
    .time-text { font-size: 14px; font-weight: 800; color: #4a5568; text-align: center; line-height: 40px; white-space: nowrap !important; margin: 0; padding: 0;}
    .empty-slot { background-color: #f8fafc; border-radius: 6px; text-align: center; color: #a0aec0; font-size: 13px; font-weight: 600; line-height: 38px; border: 1px dashed #cbd5e0; margin: 0; height: 40px; }
    /* 📌 상단 고정 영역: 시계 + 날짜선택 + 범례 (Streamlit 메뉴바 3.75rem 바로 아래에 붙음) */
    /*    Streamlit이 컨테이너를 같은 높이의 stLayoutWrapper로 감싸므로 sticky는 그 래퍼에 적용해야 동작함 */
    div[data-testid="stLayoutWrapper"]:has(> .st-key-sticky_top) { position: sticky; top: 3.75rem; z-index: 990; }
    .st-key-sticky_top { background-color: #f7f9fc; padding: 4px 0 2px 0; gap: 6px !important; }
    .st-key-sticky_top .legend-box { margin-bottom: 4px; }
    /* 모바일 타임라인용 3열 그리드(50px/1fr/45px)가 이 줄에 적용되지 않도록 덮어씀 */
    .st-key-sticky_top div[data-testid="stHorizontalBlock"] { display: grid !important; grid-template-columns: 1fr 64px !important; gap: 6px !important; margin-bottom: 0 !important; }
    .st-key-sticky_top div[data-testid="stColumn"] { width: 100% !important; min-width: 0 !important; }
    .st-key-sticky_top iframe { display: block; width: 100% !important; }
    .st-key-sticky_top div[data-testid="stPopover"], .st-key-sticky_top div[data-testid="stPopover"] > div { width: 100% !important; }
    div[data-testid="stPopover"] button { width: 100% !important; min-height: 40px !important; height: 40px; padding: 0 4px !important; border-radius: 8px !important; }
    div[data-testid="stPopover"] button p { font-size: 15px !important; font-weight: 700 !important; white-space: nowrap !important; }
    .viewing-date { background: #fffbea; border: 1px solid #f6e05e; color: #744210; border-radius: 6px; font-size: 12px; font-weight: 700; text-align: center; padding: 4px; }
    .full-slot { background-color: #fff5f5; border-radius: 6px; text-align: center; color: #c53030; font-size: 12px; font-weight: 700; line-height: 38px; border: 1px solid #feb2b2; margin: 0; height: 40px; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">🚜 정비고 입출고 관리</div>', unsafe_allow_html=True)

# 💡 상단 고정 영역 (시계 + 📅 날짜 선택 + 범례) — CSS .st-key-sticky_top 으로 sticky 처리
today_kst = datetime.now(KST).date()
WEEKDAYS = "월화수목금토일"

with st.container(key="sticky_top"):
    col_clock, col_cal = st.columns([6, 1], vertical_alignment="center")

    with col_clock:
        # 현재시간 시계: 브라우저에서 1초마다 갱신 (앱 재실행 없이 움직임), 항상 한국시간 기준
        components.html("""
<style>html, body { margin: 0; padding: 0; overflow: hidden; }</style>
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#1a202c;color:#fff;border-radius:8px;height:40px;box-sizing:border-box;
     display:flex;justify-content:center;align-items:center;gap:8px;">
  <span id="clock-date" style="font-size:12px;color:#cbd5e0;"></span>
  <span id="clock-time" style="font-size:19px;font-weight:800;letter-spacing:1px;font-variant-numeric:tabular-nums;"></span>
</div>
<script>
  const dFmt = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul', month:'long', day:'numeric', weekday:'short'});
  const tFmt = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
  function tick() {
    const now = new Date();
    document.getElementById('clock-date').textContent = dFmt.format(now);
    document.getElementById('clock-time').textContent = tFmt.format(now);
  }
  tick(); setInterval(tick, 1000);
</script>
""", height=40)

    with col_cal:
        # 📅 아이콘을 누르면 달력이 열림. 오늘이 아닌 날짜를 보고 있으면 아이콘 옆에 그 날짜를 표시
        sel = st.session_state.get("target_date", today_kst)
        cal_label = "📅" if sel == today_kst else f"📅{sel.month}/{sel.day}"
        with st.popover(cal_label, use_container_width=True):
            target_date = st.date_input("조회 날짜", today_kst, key="target_date")

    if target_date != today_kst:
        st.markdown(
            f"<div class='viewing-date'>📌 {target_date.month}월 {target_date.day}일"
            f"({WEEKDAYS[target_date.weekday()]}) 예약을 보고 있습니다</div>",
            unsafe_allow_html=True,
        )

    legend_html = "".join([
        f"<span style='white-space:nowrap;'>{v['icon']} {k}({'도크' if v['pool'] == 'dock' else ''}{v['limit']})</span>"
        for k, v in TASK_CONFIG.items()
    ])
    legend_html += f"<span style='white-space:nowrap;'>｜ 📅사전 ⚡당일 ⏸️부품대기 ｜ 일반 슬롯총량 {SLOT_TOTAL_LIMIT}대</span>"
    st.markdown(f'<div class="legend-box">{legend_html}</div>', unsafe_allow_html=True)

try:
    df_all = conn.read(worksheet="Sheet1", ttl=0)
    df_all = df_all.dropna(subset=['id'])

    if not df_all.empty and 'date' in df_all.columns:
        df_all['date'] = pd.to_datetime(df_all['date'], format='mixed', errors='coerce').dt.strftime('%Y-%m-%d')
        df_all['start_time'] = pd.to_datetime(df_all['start_time'].astype(str), format='mixed', errors='coerce').dt.strftime('%H:%M')
        df_all['end_time'] = pd.to_datetime(df_all['end_time'].astype(str), format='mixed', errors='coerce').dt.strftime('%H:%M')

        # 💡 핵심 픽스: 소수점(.0) 제거 및 방어용으로 붙였던 싱글 쿼테이션(')을 다시 깔끔하게 제거
        if 'vehicle_no' in df_all.columns:
            df_all['vehicle_no'] = df_all['vehicle_no'].astype(str).str.replace(r'\.0$', '', regex=True).str.replace(r"^\'", "", regex=True).replace('nan', '')

        # created_at 컬럼이 없으면(과거 데이터) 빈 값으로 채워 미분류 처리
        if 'created_at' not in df_all.columns:
            df_all['created_at'] = ""
        if 'status' not in df_all.columns:
            df_all['status'] = ""
        df_all['status'] = df_all['status'].fillna("").astype(str)

        df_res = df_all[df_all['date'] == str(target_date)]
    else:
        df_res = pd.DataFrame()
except Exception as e:
    st.error(f"구글 스프레드시트 연결 오류의 진짜 원인: {e}")
    df_res = pd.DataFrame()

# --- 타임라인 렌더링 ---
for slot in slots:
    if df_res.empty:
        current_tasks = pd.DataFrame()
        slot_total = 0
    else:
        current_tasks = df_res[(df_res['start_time'] <= slot) & (df_res['end_time'] > slot)]
        # 💡 여러 슬롯에 걸친 예약은 첫 점유 슬롯(입고 시간)에만 표시 → 목록 간소화
        #    (정원 계산은 아래 count_slot에서 걸치는 모든 슬롯에 그대로 반영됨)
        current_tasks = current_tasks[
            current_tasks.apply(lambda r: first_slot_of(r['start_time'], r['end_time']) == slot, axis=1)
        ] if not current_tasks.empty else current_tasks

    # 💡 정원제: 어떤 유형도 더 받을 수 없을 때만 마감 처리
    slot_counts, slot_general = count_slot(df_res, slot)
    is_full = not any(slot_has_room(slot_counts, slot_general, t) for t in TASK_CATEGORIES)

    col_time, col_tasks, col_add = st.columns([1.5, 7, 1.5])

    with col_time:
        st.markdown(f"<div class='time-text'>{slot}</div>", unsafe_allow_html=True)

    with col_tasks:
        if current_tasks.empty:
            if is_full:
                st.markdown("<div class='full-slot'>마감</div>", unsafe_allow_html=True)
            else:
                st.markdown("<div class='empty-slot'>예약 가능</div>", unsafe_allow_html=True)
        else:
            for _, row in current_tasks.iterrows():
                config = TASK_CONFIG.get(row['task_type'], {"icon": "📌"})
                # 💡 사전/당일 뱃지 판별
                booking_label, badge = classify_booking(row.get('created_at'), row['date'])
                wait_badge = "⏸️" if row.get('status') == STATUS_WAITING else ""
                btn_label = f"{wait_badge}{badge}{config['icon']} {row['task_type']} : {row['vehicle_no']}"
                # 1시간 이상 예약은 입고~출고 시간 범위를 함께 표시
                if booking_minutes(row['start_time'], row['end_time']) > 30:
                    btn_label += f" ({row['start_time']}~{row['end_time']})"
                if st.button(btn_label, key=f"view_{row['id']}_{slot}", use_container_width=True):
                    safe_details = str(row['details']) if pd.notna(row.get('details')) else ""
                    reservation_modal(row['id'], row['vehicle_no'], row['manager'], row['task_type'],
                                      row['start_time'], row['end_time'], safe_details,
                                      row.get('status', ""), target_date, booking_label)

    with col_add:
        if is_full:
            # 💡 정원제: 총량 마감 시 신규 예약 차단
            st.button("🔒", key=f"full_{slot}", disabled=True, use_container_width=True)
        else:
            if st.button("➕", key=f"add_{slot}", type="primary", use_container_width=True):
                create_reservation_modal(target_date, slot, df_res)
