import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, timedelta

# 1. DB 초기화 (버전 6.0으로 통합)
def init_db():
    conn = sqlite3.connect('maintenance_db', check_same_thread=False)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            start_time TEXT,
            end_time TEXT,
            vehicle_no TEXT,
            manager TEXT,
            task_type TEXT,
            details TEXT
        )
    ''')
    conn.commit()
    return conn

# 💡 오전/오후 업무시간 설정 (점심시간 제외)
def get_display_slots():
    morning = [f"{h:02d}:{m:02d}" for h in range(8, 12) for m in (0, 30)]
    afternoon = [f"{h:02d}:{m:02d}" for h in range(13, 17) for m in (0, 30)]
    return morning + afternoon

TASK_CATEGORIES = ["정비점검", "오일교환", "요소수교환", "경정비"]
TASK_CONFIG = {
    "정비점검": {"color": "#FF4B4B", "icon": "🔧"},
    "오일교환": {"color": "#FFA500", "icon": "🛢️"},
    "요소수교환": {"color": "#1E90FF", "icon": "💦"},
    "경정비": {"color": "#28A745", "icon": "📦"}
}

st.set_page_config(page_title="모바일 정비예약", layout="wide")
conn = init_db()
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
    
    # 30분 자동 계산 로직
    start_dt = datetime.strptime(start_time, "%H:%M")
    e_time = (start_dt + timedelta(minutes=30)).strftime("%H:%M")
    
    st.caption(f"⏱️ 출고 시간은 **{e_time}**으로 자동 설정됩니다.")

    if st.button("예약 저장", type="primary", use_container_width=True):
        if not v_no or not m_name:
            st.error("차량 번호와 운전자를 입력해주세요.")
        elif task_type == "정비점검" and not details.strip():
            st.warning("점검사항을 기입해주세요.")
        else:
            c = conn.cursor()
            c.execute("INSERT INTO reservations (date, start_time, end_time, vehicle_no, manager, task_type, details) VALUES (?,?,?,?,?,?,?)",
                      (str(selected_date), start_time, e_time, v_no, m_name, task_type, details))
            conn.commit()
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
    
    # 수정한 입고 시간에 맞춰 출고 시간도 +30분 자동 계산
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
                c = conn.cursor()
                c.execute("UPDATE reservations SET start_time=?, end_time=?, vehicle_no=?, manager=?, task_type=?, details=? WHERE id=?",
                          (new_s_time, new_e_time, new_v_no, new_manager, new_task_type, new_details, res_id))
                conn.commit()
                st.rerun()
    with col_delete:
        if st.button("🗑️ 삭제", type="secondary", use_container_width=True):
            c = conn.cursor()
            c.execute("DELETE FROM reservations WHERE id=?", (res_id,))
            conn.commit()
            st.rerun()

# --- 📱 모바일 절대 방어 및 모달창 버그 픽스 CSS ---
st.markdown("""
<style>
    .stApp { background-color: #f7f9fc; }
    
    .block-container {
        padding: 1rem 0.5rem 2rem 0.5rem !important; 
        max-width: 100% !important; 
    }
    
    .main-title { font-size: 20px; font-weight: 800; color: #1a202c; text-align: center; margin-bottom: 10px; }
    
    .legend-box {
        background: #ffffff; padding: 8px; border-radius: 8px; font-size: 11px;
        color: #4a5568; display: flex; flex-wrap: wrap; justify-content: center; gap: 6px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05); margin-bottom: 15px; border: 1px solid #e2e8f0;
    }
    
    @media (max-width: 640px) {
        div[data-testid="stHorizontalBlock"] {
            display: grid !important;
            grid-template-columns: 50px 1fr 45px !important;
            gap: 5px !important;
            width: 100% !important;
            margin-bottom: 6px !important;
        }
        div[data-testid="column"] {
            width: 100% !important;
            min-width: 0 !important;
        }
        
        div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] {
            display: flex !important;
            flex-direction: row !important;
            gap: 10px !important;
        }
        div[data-testid="stDialog"] div[data-testid="column"] {
            width: 50% !important;
            flex: 1 1 50% !important;
        }
    }
    
    div[data-testid="stButton"] button {
        border-radius: 6px !important; 
        min-height: 40px !important; 
        padding: 0 !important; 
        width: 100% !important;
        overflow: hidden !important;
    }
    div[data-testid="stButton"] button p { 
        font-size: 13px !important; font-weight: 600 !important; margin: 0 !important; 
        white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; 
    }
    
    button[kind="secondary"] {
        background-color: white !important;
        border: 1px solid #e2e8f0 !important;
    }
    button[kind="secondary"] p { color: #1a202c !important; }
    
    button[kind="primary"] { border: none !important; }
    button[kind="primary"] p { color: white !important; }
    
    .time-text { font-size: 14px; font-weight: 800; color: #4a5568; text-align: center; line-height: 40px; white-space: nowrap !important; margin: 0; padding: 0;}
    
    .empty-slot {
        background-color: #f8fafc; border-radius: 6px; text-align: center; 
        color: #a0aec0; font-size: 13px; font-weight: 600; line-height: 38px; 
        border: 1px dashed #cbd5e0; margin: 0; height: 40px;
    }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">🚜 정비고 입출고 관리</div>', unsafe_allow_html=True)

target_date = st.date_input("조회 날짜", datetime.today(), label_visibility="collapsed")

legend_html = "".join([f"<span style='white-space:nowrap;'>{v['icon']} {k}</span>" for k, v in TASK_CONFIG.items()])
st.markdown(f'<div class="legend-box">{legend_html}</div>', unsafe_allow_html=True)

# 💡 수정된 부분: SQL 인젝션 방어 및 파라미터 바인딩 적용
query = "SELECT * FROM reservations WHERE date = ?"
df_res = pd.read_sql_query(query, conn, params=(str(target_date),))

# --- 타임라인 렌더링 ---
for slot in slots:
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
                    # 💡 수정된 부분: NaN 결측치 안전 처리 로직 적용
                    safe_details = str(row['details']) if pd.notna(row['details']) and row['details'] is not None else ""
                    reservation_modal(row['id'], row['vehicle_no'], row['manager'], row['task_type'], row['start_time'], safe_details, target_date)
                    
    with col_add:
        if st.button("➕", key=f"add_{slot}", type="primary", use_container_width=True):
            create_reservation_modal(target_date, slot)