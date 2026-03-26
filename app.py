import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, timezone # 💡 timezone 모듈 추가
import time
from streamlit_gsheets import GSheetsConnection

st.set_page_config(page_title="모바일 정비예약", layout="wide")

# 1. 구글 시트 DB 연결
conn = st.connection("gsheets", type=GSheetsConnection)

# 💡 핵심 픽스: 서버 위치와 무관하게 무조건 한국 표준시(KST) 적용
KST = timezone(timedelta(hours=9))

def get_display_slots():
    morning = [f"{h:02d}:{m:02d}" for h in range(8, 12) for m in (0, 30)]
    afternoon = [f"{h:02d}:{m:02d}" for h in range(13, 17) for m in (0, 30)]
    return morning + afternoon

TASK_CATEGORIES = ["정비점검", "오일교환", "요소수보충", "경정비"]
TASK_CONFIG = {
    "정비점검": {"color": "#FF4B4B", "icon": "🔧"},
    "오일교환": {"color": "#FFA500", "icon": "🛢️"},
    "요소수보충": {"color": "#1E90FF", "icon": "💦"},
    "경정비": {"color": "#28A745", "icon": "📦"}
}

slots = get_display_slots()

# --- 🚀 신규 예약 등록 모달창 ---
@st.dialog("➕ 신규 예약 등록")
def create_reservation_modal(selected_date, start_time):
    st.info(f"선택 시간: **{selected_date} / {start_time}**")
    v_no = st.text_input("차량 번호")
    m_name = st.text_input("운전자") 
    task_type = st.selectbox("정비 유형", TASK_CATEGORIES)
    
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
            df = conn.read(worksheet="Sheet1", ttl=0)
            df = df.dropna(subset=['id']) 
            
            new_id = int(df['id'].max() + 1) if not df.empty and pd.notna(df['id'].max()) else 1
            
            new_row = pd.DataFrame([{
                "id": new_id, "date": str(selected_date), "start_time": start_time, 
                "end_time": e_time, "vehicle_no": f"'{v_no}", "manager": m_name, 
                "task_type": task_type, "details": details
            }])
            
            updated_df = pd.concat([df, new_row], ignore_index=True)
            conn.update(worksheet="Sheet1", data=updated_df)
            
            st.cache_data.clear()
            time.sleep(1.5) 
            st.rerun()

# --- 🚀 예약 조회 / 수정 / 삭제 통합 모달창 ---
@st.dialog("📋 예약 상세 및 관리")
def reservation_modal(res_id, v_no, manager, t_type, s_time, details, selected_date):
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
                df = conn.read(worksheet="Sheet1", ttl=0)
                df = df.dropna(subset=['id'])
                idx = df.index[df['id'] == res_id].tolist()
                
                if idx:
                    df.at[idx[0], 'start_time'] = new_s_time
                    df.at[idx[0], 'end_time'] = new_e_time
                    df.at[idx[0], 'vehicle_no'] = f"'{new_v_no}"
                    df.at[idx[0], 'manager'] = new_manager
                    df.at[idx[0], 'task_type'] = new_task_type
                    df.at[idx[0], 'details'] = new_details
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
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">🚜 정비고 입출고 관리</div>', unsafe_allow_html=True)

# 💡 핵심 픽스: 오늘 날짜를 가져올 때 한국 시간(KST)을 기준으로 가져오도록 변경
today_kst = datetime.now(KST).date()
target_date = st.date_input("조회 날짜", today_kst, label_visibility="collapsed")

legend_html = "".join([f"<span style='white-space:nowrap;'>{v['icon']} {k}</span>" for k, v in TASK_CONFIG.items()])
st.markdown(f'<div class="legend-box">{legend_html}</div>', unsafe_allow_html=True)

try:
    df_all = conn.read(worksheet="Sheet1", ttl=0)
    df_all = df_all.dropna(subset=['id']) 
    
    if not df_all.empty and 'date' in df_all.columns:
        df_all['date'] = pd.to_datetime(df_all['date'], errors='coerce').dt.strftime('%Y-%m-%d')
        df_all['start_time'] = pd.to_datetime(df_all['start_time'].astype(str), errors='coerce').dt.strftime('%H:%M')
        df_all['end_time'] = pd.to_datetime(df_all['end_time'].astype(str), errors='coerce').dt.strftime('%H:%M')
        
        if 'vehicle_no' in df_all.columns:
            df_all['vehicle_no'] = df_all['vehicle_no'].astype(str).str.replace(r'\.0$', '', regex=True).str.replace(r"^\'", "", regex=True).replace('nan', '')
            
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
    else:
        current_tasks = df_res[(df_res['start_time'] <= slot) & (df_res['end_time'] > slot)]
    
    col_time, col_tasks, col_add = st.columns([1.5, 7, 1.5])
    
    with col_time:
        st.markdown(f"<div class='time-text'>{slot}</div>", unsafe_allow_html=True)
        
    with col_tasks:
        if current_tasks.empty:
            st.markdown("<div class='empty-slot'>예약 가능</div>", unsafe_allow_html=True)
        else:
            for _, row in current_tasks.iterrows():
                config = TASK_CONFIG.get(row['task_type'], {"icon": "📌"})
                btn_label = f"{config['icon']} {row['task_type']} : {row['vehicle_no']}"
                if st.button(btn_label, key=f"view_{row['id']}_{slot}", use_container_width=True):
                    safe_details = str(row['details']) if pd.notna(row.get('details')) else ""
                    reservation_modal(row['id'], row['vehicle_no'], row['manager'], row['task_type'], row['start_time'], safe_details, target_date)
                    
    with col_add:
        if st.button("➕", key=f"add_{slot}", type="primary", use_container_width=True):
            create_reservation_modal(target_date, slot)
