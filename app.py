import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import time
from streamlit_gsheets import GSheetsConnection

st.set_page_config(page_title="모바일 정비예약", layout="wide")

# 1. 구글 시트 DB 연결
conn = st.connection("gsheets", type=GSheetsConnection)

def get_display_slots():
    morning = [f"{h:02d}:{m:02d}" for h in range(8, 12) for m in (0, 30)]
    afternoon = [f"{h:02d}:{m:02d}" for h in range(13, 17) for m in (0, 30)]
    return morning + afternoon

TASK_CATEGORIES = ["정비점검", "오일교환", "요소수보충", "경정비"]

# 💡 정원제: 유형별 30분당 최대 대수(limit)를 config에 함께 정의
TASK_CONFIG = {
    "정비점검": {"color": "#FF4B4B", "icon": "🔧", "limit": 1},
    "오일교환": {"color": "#FFA500", "icon": "🛢️", "limit": 2},
    "요소수보충": {"color": "#1E90FF", "icon": "💦", "limit": 2},
    "경정비": {"color": "#28A745", "icon": "📦", "limit": 2}
}

# 💡 정원제: 한 슬롯(30분)에 동시 입고 가능한 총 최대 대수
SLOT_TOTAL_LIMIT = 4

slots = get_display_slots()


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
    return df


def count_slot(df_norm, slot):
    """정규화된 데이터에서 해당 슬롯을 점유하는 예약의 (유형별 카운트, 총 카운트) 반환."""
    if df_norm is None or df_norm.empty:
        return {}, 0
    occ = df_norm[(df_norm['start_time'] <= slot) & (df_norm['end_time'] > slot)]
    type_counts = occ['task_type'].value_counts().to_dict()
    return type_counts, len(occ)


def check_capacity(df_norm, slot, task_type, exclude_id=None):
    """
    저장 직전 정원 재검증. 통과하면 (True, ""), 초과하면 (False, 사유메시지).
    exclude_id: 수정 시 자기 자신을 카운트에서 제외하기 위한 예약 id.
    """
    df_check = df_norm
    if exclude_id is not None and df_check is not None and not df_check.empty:
        df_check = df_check[df_check['id'] != exclude_id]

    type_counts, total = count_slot(df_check, slot)

    if total >= SLOT_TOTAL_LIMIT:
        return False, f"이 시간대는 총 정원({SLOT_TOTAL_LIMIT}대)이 모두 찼습니다."

    type_limit = TASK_CONFIG.get(task_type, {}).get("limit", 99)
    if type_counts.get(task_type, 0) >= type_limit:
        return False, f"'{task_type}'은(는) 이 시간대 정원({type_limit}대)이 찼습니다. 다른 유형/시간을 선택하세요."

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
    type_counts, total = count_slot(df_res, start_time)
    remaining_total = SLOT_TOTAL_LIMIT - total

    # 아직 자리가 남은 유형만 선택지로 노출
    available_types = [
        t for t in TASK_CATEGORIES
        if type_counts.get(t, 0) < TASK_CONFIG[t]["limit"]
    ]

    if remaining_total <= 0 or not available_types:
        st.error(f"이 시간대는 예약이 마감되었습니다. (총 정원 {SLOT_TOTAL_LIMIT}대)")
        return

    # 잔여 현황 안내
    remain_txt = " / ".join(
        [f"{TASK_CONFIG[t]['icon']}{t} {TASK_CONFIG[t]['limit'] - type_counts.get(t, 0)}"
         for t in available_types]
    )
    st.caption(f"🅿️ 이 시간대 잔여 — 총 **{remaining_total}대** ｜ {remain_txt}")

    v_no = st.text_input("차량 번호")
    m_name = st.text_input("운전자")
    task_type = st.selectbox("정비 유형", available_types)

    details = ""
    if task_type == "정비점검":
        details = st.text_area("📝 점검사항 (필수 기입)")

    start_dt = datetime.strptime(start_time, "%H:%M")
    e_time = (start_dt + timedelta(minutes=30)).strftime("%H:%M")
    st.caption(f"⏱️ 출고 시간은 **{e_time}**으로 자동 설정됩니다.")

    if st.button("예약 저장", type="primary", use_container_width=True):
        if not v_no or not m_name:
            st.error("차량 번호와 운전자를 입력해주세요.")
        elif task_type == "정비점검" and not details.strip():
            st.warning("점검사항을 기입해주세요.")
        else:
            df_raw = conn.read(worksheet="Sheet1", ttl=0)
            df = df_raw.dropna(subset=['id']) if df_raw is not None else pd.DataFrame()

            # 💡 정원제: 저장 직전 최신 데이터로 정원 재검증 (동시 저장 방어)
            df_norm_all = normalize_df(df_raw)
            df_norm_day = df_norm_all[df_norm_all['date'] == str(selected_date)] if not df_norm_all.empty else pd.DataFrame()
            ok, reason = check_capacity(df_norm_day, start_time, task_type)
            if not ok:
                st.error(f"⛔ {reason}")
                st.stop()

            new_id = int(df['id'].max() + 1) if not df.empty and pd.notna(df['id'].max()) else 1

            # 💡 사전/당일 구분용: 등록 시각 기록
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 💡 핵심 픽스: 구글 시트의 숫자 강제 변환을 막기 위해 앞에 싱글 쿼테이션(') 주입
            new_row = pd.DataFrame([{
                "id": new_id, "date": str(selected_date), "start_time": start_time,
                "end_time": e_time, "vehicle_no": f"'{v_no}", "manager": m_name,
                "task_type": task_type, "details": details, "created_at": created_at
            }])

            updated_df = pd.concat([df, new_row], ignore_index=True)
            conn.update(worksheet="Sheet1", data=updated_df)

            st.cache_data.clear()
            time.sleep(1.5)
            st.rerun()


# --- 🚀 예약 조회 / 수정 / 삭제 통합 모달창 ---
@st.dialog("📋 예약 상세 및 관리")
def reservation_modal(res_id, v_no, manager, t_type, s_time, details, selected_date, booking_label):
    # 💡 사전/당일 라벨 표시
    st.caption(f"구분: **{booking_label} 예약**")

    new_v_no = st.text_input("차량 번호", value=v_no)
    new_manager = st.text_input("운전자", value=manager)

    type_idx = TASK_CATEGORIES.index(t_type) if t_type in TASK_CATEGORIES else 0
    new_task_type = st.selectbox("정비 유형", TASK_CATEGORIES, index=type_idx)

    new_details = details
    if new_task_type == "정비점검":
        new_details = st.text_area("📝 점검사항", value=details if details else "")

    s_idx = slots.index(s_time) if s_time in slots else 0
    new_s_time = st.selectbox("입고 시간", slots, index=s_idx)

    start_dt = datetime.strptime(new_s_time, "%H:%M")
    new_e_time = (start_dt + timedelta(minutes=30)).strftime("%H:%M")
    st.caption(f"⏱️ 출고 시간은 **{new_e_time}**으로 자동 변경됩니다.")

    st.divider()
    col_update, col_delete = st.columns(2)
    with col_update:
        if st.button("💾 저장", type="primary", use_container_width=True):
            if not new_v_no or not new_manager:
                st.error("차량 번호와 운전자를 입력해주세요.")
            else:
                df_raw = conn.read(worksheet="Sheet1", ttl=0)
                df = df_raw.dropna(subset=['id'])

                # 💡 정원제: 수정 시에도 재검증하되, 자기 자신(res_id)은 카운트에서 제외
                df_norm_all = normalize_df(df_raw)
                df_norm_day = df_norm_all[df_norm_all['date'] == str(selected_date)] if not df_norm_all.empty else pd.DataFrame()
                ok, reason = check_capacity(df_norm_day, new_s_time, new_task_type, exclude_id=res_id)
                if not ok:
                    st.error(f"⛔ {reason}")
                    st.stop()

                idx = df.index[df['id'] == res_id].tolist()

                if idx:
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
    .block-container { padding: 1rem 0.5rem 2rem 0.5rem !important; max-width: 100% !important; }
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
    .full-slot { background-color: #fff5f5; border-radius: 6px; text-align: center; color: #c53030; font-size: 12px; font-weight: 700; line-height: 38px; border: 1px solid #feb2b2; margin: 0; height: 40px; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">🚜 정비고 입출고 관리</div>', unsafe_allow_html=True)

target_date = st.date_input("조회 날짜", datetime.today(), label_visibility="collapsed")

legend_html = "".join([f"<span style='white-space:nowrap;'>{v['icon']} {k}({v['limit']})</span>" for k, v in TASK_CONFIG.items()])
legend_html += f"<span style='white-space:nowrap;'>｜ 📅사전 ⚡당일 ｜ 슬롯총량 {SLOT_TOTAL_LIMIT}대</span>"
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
        _, slot_total = count_slot(df_res, slot)

    is_full = slot_total >= SLOT_TOTAL_LIMIT

    col_time, col_tasks, col_add = st.columns([1.5, 7, 1.5])

    with col_time:
        st.markdown(f"<div class='time-text'>{slot}</div>", unsafe_allow_html=True)

    with col_tasks:
        if current_tasks.empty:
            st.markdown("<div class='empty-slot'>예약 가능</div>", unsafe_allow_html=True)
        else:
            for _, row in current_tasks.iterrows():
                config = TASK_CONFIG.get(row['task_type'], {"icon": "📌"})
                # 💡 사전/당일 뱃지 판별
                booking_label, badge = classify_booking(row.get('created_at'), row['date'])
                btn_label = f"{badge}{config['icon']} {row['task_type']} : {row['vehicle_no']}"
                if st.button(btn_label, key=f"view_{row['id']}_{slot}", use_container_width=True):
                    safe_details = str(row['details']) if pd.notna(row.get('details')) else ""
                    reservation_modal(row['id'], row['vehicle_no'], row['manager'], row['task_type'],
                                      row['start_time'], safe_details, target_date, booking_label)

    with col_add:
        if is_full:
            # 💡 정원제: 총량 마감 시 신규 예약 차단
            st.button("🔒", key=f"full_{slot}", disabled=True, use_container_width=True)
        else:
            if st.button("➕", key=f"add_{slot}", type="primary", use_container_width=True):
                create_reservation_modal(target_date, slot, df_res)
