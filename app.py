import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, timezone
import time
import streamlit.components.v1 as components
from streamlit_gsheets import GSheetsConnection

# 💡 시간대: 한국 표준시(KST, UTC+9) 고정 오프셋. (한국은 서머타임 없음 → 항상 +9)
#    서버가 UTC여도 등록 시각/조회 날짜가 한국 시간 기준이 되도록 강제.
KST = timezone(timedelta(hours=9))

st.set_page_config(page_title="정비고 입출고 관리", layout="wide")

# 1. 구글 시트 DB 연결
conn = st.connection("gsheets", type=GSheetsConnection)

# 💡 직전 동작 결과 안내: st.session_state["flash"] = {"msg", "icon"} 에 보관했다가
#    화면 상단 고정 영역 아래에 배너로 한 번 표시함 (표시는 타임라인 영역에서 처리)


def sheet_failure(action, err):
    """시트 읽기/쓰기 실패 시: 무엇이 안 됐는지 + 다음 행동을 알리고 이후 화면을 멈춤."""
    st.error(f"**{action}하지 못했습니다.** 인터넷 연결을 확인한 뒤 다시 시도해주세요. "
             "입력한 내용은 시트에 반영되지 않았습니다.")
    with st.expander("기술 정보 (관리자용)"):
        st.code(f"{type(err).__name__}: {err}")
    st.stop()

def get_display_slots():
    morning = [f"{h:02d}:{m:02d}" for h in range(8, 12) for m in (0, 30)]
    afternoon = [f"{h:02d}:{m:02d}" for h in range(13, 17) for m in (0, 30)]
    return morning + afternoon

TASK_CATEGORIES = ["차량정비", "마이카", "오일교환", "요소수보충", "정비점검", "타이어점검", "경정비", "기타"]

# 💡 정원제: 유형별 30분당 최대 대수(limit) / 선택 가능한 소요시간(분) / 정원 풀(pool)
#   pool "general": 잠깐 들르는 차량 → SLOT_TOTAL_LIMIT(총 정원)에 포함
#   pool "dock"   : 차량정비(장시간) → 정비도크 수(limit)로만 별도 관리
#   pool "mycar"  : 마이카 → 자체 limit로만 별도 관리 (총 정원에 포함 안 됨)
TASK_CONFIG = {
    "차량정비": {"color": "#6B46C1", "icon": "🛠️", "limit": 4, "durations": list(range(60, 421, 30)), "pool": "dock"},
    "마이카": {"color": "#D53F8C", "icon": "🚗", "limit": 2, "durations": [30, 60], "pool": "mycar"},
    "오일교환": {"color": "#FFA500", "icon": "🛢️", "limit": 1, "durations": [30], "pool": "general"},
    "요소수보충": {"color": "#1E90FF", "icon": "💦", "limit": 1, "durations": [30], "pool": "general"},
    "정비점검": {"color": "#FF4B4B", "icon": "🔧", "limit": 1, "durations": [30], "pool": "general"},
    "타이어점검": {"color": "#4A5568", "icon": "🛞", "limit": 1, "durations": [30], "pool": "general"},
    "경정비": {"color": "#28A745", "icon": "📦", "limit": 1, "durations": [30], "pool": "general"},
    "기타": {"color": "#718096", "icon": "📌", "limit": 1, "durations": [30, 60], "pool": "general"},
}

# 상세내용 입력란을 보여줄 유형 / 그중 필수 입력 유형
DETAIL_TYPES = ["정비점검", "차량정비", "경정비", "기타"]
DETAIL_REQUIRED = ["정비점검", "차량정비", "경정비", "기타"]

# 💡 정원제: 한 슬롯(30분)에 동시 입고 가능한 총 최대 대수 (general 풀에만 적용 — 차량정비·마이카 제외)
SLOT_TOTAL_LIMIT = 2

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


import html as _html

# 저장 후 시트 반영을 기다리는 짧은 대기 (Sheets API는 쓰기 응답 후 곧바로 읽기가 일관됨 → 안전 여유만)
SAVE_SETTLE_SEC = 0.3

# 신규 예약에서 바로 보여줄 유형 (사용 빈도순 — 차량정비가 정비고의 본업). 나머지는 '더보기'
PRIMARY_TYPES = ["차량정비", "오일교환", "마이카", "요소수보충", "정비점검"]
MORE_OPTION = "더보기"
FULL_MARK = " (마감)"

# 차량정비 소요시간 칩: 자주 쓰는 길이 + 종일 + 기타(정확한 값 선택)
DOCK_CHIP_MINUTES = [60, 120, 180, 240]
DUR_DAY = "종일"
DUR_OTHER = "기타"

WEEKDAYS_KO = "월화수목금토일"

# 보기 화면 유형 점 색 (흰 바탕 대비를 위해 밝은 색은 진하게 — 타임라인 색과 동일 계열)
RV_TYPE_COLORS = {"오일교환": "#B86E00", "요소수보충": "#1C7ED6", "정비점검": "#E03131", "경정비": "#2F855A"}


def human_date(selected_date):
    """'2026-11-17' → '11/17(화)'. 해석 불가하면 원문 그대로."""
    try:
        d = datetime.strptime(str(selected_date)[:10], "%Y-%m-%d")
    except ValueError:
        return str(selected_date)
    return f"{d.month}/{d.day}({WEEKDAYS_KO[d.weekday()]})"


def human_datetime(selected_date, start_time):
    return f"{human_date(selected_date)} {start_time}"


def normalize_vehicle_no(v_no):
    """차량번호: 앞뒤 공백 제거 + 중간 연속 공백을 한 칸으로."""
    return " ".join(str(v_no or "").split())


def is_past_slot(selected_date, start_time):
    """오늘(KST) 날짜에서 이미 끝난 30분 슬롯이면 True. (진행 중인 현재 슬롯은 허용 — 즉시 입고)"""
    now = datetime.now(KST)
    if str(selected_date) != now.date().isoformat() or not start_time:
        return False
    slot_end = (datetime.strptime(start_time, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
    return slot_end <= now.strftime("%H:%M")


def validate_booking(v_no, manager, task_type, details, e_time,
                     selected_date=None, start_time=None, check_past=True, minutes="n/a"):
    """신규/수정 공통 입력 검증. 문제가 있으면 안내 문구, 없으면 None.
    minutes=None 이면 소요시간 미선택으로 간주 (기본값 'n/a'는 검사 생략)."""
    if not normalize_vehicle_no(v_no) or not str(manager or "").strip():
        return "차량 번호와 운전자를 입력해주세요."
    if not task_type:
        return "정비 유형을 선택하세요"
    if minutes is None:
        return "소요시간을 선택하세요"
    if task_type in DETAIL_REQUIRED and not str(details or "").strip():
        return f"'{task_type}'은(는) 상세내용을 꼭 기입해주세요."
    if not e_time:
        return "운영시간(17:00)을 넘기는 예약은 저장할 수 없습니다. 소요시간을 줄이거나 입고 시간을 앞당기세요."
    if check_past and is_past_slot(selected_date, start_time):
        return f"{start_time} 은(는) 이미 지난 시간입니다. 지금 이후 시간을 선택하세요."
    return None


def load_day_df(selected_date, ttl=0):
    """해당 날짜 예약만 정규화해서 반환 + 원본(df_raw). 실패 시 sheet_failure로 멈춤."""
    try:
        df_raw = conn.read(worksheet="Sheet1", ttl=ttl)
    except Exception as e:
        sheet_failure("최신 예약 현황을 불러오지", e)
    df_norm_all = normalize_df(df_raw)
    df_day = df_norm_all[df_norm_all['date'] == str(selected_date)] if not df_norm_all.empty else pd.DataFrame()
    return df_raw, df_day


def booking_summary(v_no, t_type, s_time, e_time):
    return f"{v_no} · {t_type} · {s_time}~{e_time}"


def finish_save(msg, icon, booking_id):
    """저장 성공 공통 처리: 알림 + 그리드 하이라이트 예약 + 새로고침."""
    st.session_state["flash"] = {"msg": msg, "icon": icon}
    try:
        st.session_state["highlight_id"] = int(float(booking_id))
    except (TypeError, ValueError):
        st.session_state["highlight_id"] = booking_id
    st.cache_data.clear()
    time.sleep(SAVE_SETTLE_SEC)
    st.rerun()


def type_label(t):
    return f"{TASK_CONFIG[t]['icon']} {t}"


def day_minutes(task_type, start_time):
    """종일 = 이 입고 시간부터 17:00 전에 끝나는 가장 긴 소요시간. 하나도 안 맞으면 None."""
    fits = [m for m in TASK_CONFIG[task_type]["durations"] if slot_range(start_time, m)]
    return max(fits) if fits else None


def duration_picker(label, task_type, start_time, is_full, key, default_minutes=None):
    """
    소요시간 선택 칩. 반환: 분(int) 또는 미선택 시 None.
      - 소요시간이 1개뿐인 유형: 컨트롤 없이 그 값
      - 차량정비: 1~4시간 / 종일(~17:00) / 기타(정확한 값 선택상자)
      - 2개짜리(마이카·기타): 30분 / 1시간
    is_full(minutes) → True면 라벨에 (마감) 표시 (17:00 초과 또는 정원 초과).
    """
    durations = TASK_CONFIG[task_type]["durations"]
    if len(durations) == 1:
        return durations[0]

    def mark(m):
        return FULL_MARK if is_full(m) else ""

    if TASK_CONFIG[task_type]["pool"] == "dock":
        day_m = day_minutes(task_type, start_time)
        options = [m for m in DOCK_CHIP_MINUTES if m in durations]
        if day_m:
            options.append(DUR_DAY)
        options.append(DUR_OTHER)

        def fmt(o):
            if o == DUR_DAY:
                return f"종일(~17:00){mark(day_m)}"
            if o == DUR_OTHER:
                return "기타"
            return duration_label(o) + mark(o)

        if default_minutes is None:
            default = None
        elif default_minutes in options:
            default = default_minutes
        elif day_m and default_minutes == day_m:
            default = DUR_DAY
        else:
            default = DUR_OTHER
        picked = st.pills(label, options, default=default, format_func=fmt, key=key)
        if picked == DUR_DAY:
            return day_m
        if picked == DUR_OTHER:
            idx = durations.index(default_minutes) if default_minutes in durations else None
            return st.selectbox("정확한 소요시간", durations, index=idx, placeholder="소요시간 선택",
                                key=f"{key}_x", format_func=lambda m: duration_label(m) + mark(m))
        return picked

    default = default_minutes if default_minutes in durations else durations[0]
    return st.pills(label, durations, default=default,
                    format_func=lambda m: duration_label(m) + mark(m), key=key)


# --- 🚀 신규 예약 등록 모달창 ---
@st.dialog("➕ 신규 예약 등록")
def create_reservation_modal(selected_date, start_time, df_res):
    st.info(f"선택 시간: **{human_datetime(selected_date, start_time)}**")

    # 💡 정원제: 이 슬롯의 현재 점유 상황 계산
    type_counts, general_total = count_slot(df_res, start_time)
    remaining_total = max(SLOT_TOTAL_LIMIT - general_total, 0)

    # 아직 자리가 남은 유형만 선택지로 노출
    available_types = [t for t in TASK_CATEGORIES if slot_has_room(type_counts, general_total, t)]

    if not available_types:
        st.error("이 시간대는 예약이 마감되었습니다.")
        return

    if is_past_slot(selected_date, start_time):
        st.warning(f"{start_time} 은(는) 이미 지난 시간이라 예약할 수 없습니다.")

    v_no = st.text_input("차량 번호")
    m_name = st.text_input("운전자")

    # 빈도순 유형은 한 번에 탭 / 나머지는 '더보기'. 기본 선택 없음 → 실수로 엉뚱한 유형 저장 방지
    primary = [t for t in PRIMARY_TYPES if t in available_types]
    others = [t for t in available_types if t not in primary]
    pill_options = primary + ([MORE_OPTION] if others else [])
    picked = st.pills(
        "정비 유형", pill_options, default=None, key="new_type",
        format_func=lambda t: "➕ 더보기" if t == MORE_OPTION else type_label(t),
    )
    if picked == MORE_OPTION:
        task_type = st.selectbox("다른 유형", others, index=None, placeholder="유형 선택",
                                 key="new_type_more", format_func=type_label)
    else:
        task_type = picked

    minutes, e_time, details = None, None, ""
    if task_type:
        cfg = TASK_CONFIG[task_type]
        left = cfg["limit"] - type_counts.get(task_type, 0)
        if cfg["pool"] == "general":
            left = min(left, remaining_total)
            st.caption(f"🅿️ {task_type} — 이 시간 {left}자리 남음 (일반 총 {remaining_total}대 남음)")
        elif cfg["pool"] == "dock":
            st.caption(f"🅿️ {task_type} — 이 시간 정비도크 {left}자리 남음")
        else:
            st.caption(f"🅿️ {task_type} — 이 시간 {left}자리 남음")

        minutes = duration_picker(
            "⏳ 예상 소요시간", task_type, start_time,
            is_full=lambda m: not check_capacity(df_res, start_time, task_type, m)[0],
            key=f"new_dur_{task_type}",
        )

        if task_type in DETAIL_TYPES:
            req = " (필수 기입)" if task_type in DETAIL_REQUIRED else ""
            details = st.text_area(f"📝 상세내용{req}")

        if minutes:
            occupied = slot_range(start_time, minutes)
            if occupied:
                e_time = end_time_of(occupied)
                st.caption(f"⏱️ 출고 예정 **{e_time}** ({duration_label(minutes)}, 점심시간 제외)")
            else:
                st.warning("운영시간(17:00)을 넘깁니다. 소요시간을 줄이거나 더 이른 시간을 선택하세요.")

    err_box = st.empty()  # 오류는 저장 버튼 바로 위에 표시
    if st.button("예약 저장", type="primary", use_container_width=True):
        v_no = normalize_vehicle_no(v_no)
        m_name = str(m_name or "").strip()
        err = validate_booking(v_no, m_name, task_type, details, e_time,
                               selected_date=selected_date, start_time=start_time, minutes=minutes)
        if err:
            err_box.error(err)
            st.stop()

        with st.spinner("저장 중…"):
            # 💡 정원제: 저장 직전 최신 데이터로 정원 재검증 (동시 저장 방어)
            df_raw, df_norm_day = load_day_df(selected_date)
            ok, reason = check_capacity(df_norm_day, start_time, task_type, minutes)
            if not ok:
                err_box.error(f"⛔ {reason}")
                st.stop()

            df = df_raw.dropna(subset=['id']) if df_raw is not None else pd.DataFrame()
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
            try:
                conn.update(worksheet="Sheet1", data=updated_df)
            except Exception as e:
                sheet_failure("예약을 저장", e)

        finish_save(f"예약됨: {booking_summary(v_no, task_type, start_time, e_time)}", "✅", new_id)


def _edit_widget_keys(res_id):
    return [f"edit_{n}_{res_id}" for n in ("vno", "mgr", "type", "start", "details", "wait")]


def _clear_edit_state(res_id):
    for k in _edit_widget_keys(res_id):
        st.session_state.pop(k, None)
    # 유형별 소요시간 칩/선택상자 키도 초기화 (수정 모드에 들어올 때마다 현재 예약값으로 시작)
    for k in [k for k in st.session_state.keys() if str(k).startswith(f"edit_dur_{res_id}_")]:
        st.session_state.pop(k, None)


_RV_STYLE = (
    "<style>"
    ".rv-vno{font-size:28px;font-weight:800;color:#1a202c;line-height:1.2;margin:0 0 8px 0;word-break:break-all;}"
    ".rv-chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px 0;}"
    ".rv-chip{display:inline-block;font-size:13px;font-weight:700;line-height:1;padding:6px 10px;"
    "border-radius:999px;border:1px solid #CED4DA;background:#F1F3F5;color:#343A40;}"
    ".rv-chip-work{background:#E6F4EA;color:#1E5631;border-color:#A7D3B5;}"
    ".rv-chip-wait{background:#FFF4E5;color:#8A4B00;border-color:#F5C78A;}"
    ".rv-grid{display:grid;grid-template-columns:max-content minmax(0,1fr);column-gap:16px;row-gap:10px;"
    "font-size:15px;line-height:1.4;margin:0 0 14px 0;}"
    ".rv-l{color:#5A6474;font-weight:600;white-space:nowrap;}"
    ".rv-v{color:#1a202c;font-weight:600;word-break:break-word;}"
    ".rv-sub{color:#5A6474;font-weight:600;}"
    ".rv-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:1px;}"
    "</style>"
)


def _reservation_read_view(res_id, v_no, manager, t_type, s_time, e_time, details,
                           waiting, is_dock, cur_minutes, selected_date, booking_label, mode_key):
    esc = _html.escape
    cfg = TASK_CONFIG.get(t_type, {})
    color = RV_TYPE_COLORS.get(t_type, cfg.get("color", "#718096"))
    icon = cfg.get("icon", "📌")

    chips = []
    if is_dock:
        chips.append("<span class='rv-chip rv-chip-wait'>⏸️ 부품대기</span>" if waiting
                     else "<span class='rv-chip rv-chip-work'>작업 중</span>")
    label_icon = {"사전": "📅 ", "당일": "⚡ "}.get(str(booking_label), "")
    chips.append(f"<span class='rv-chip'>{label_icon}{esc(str(booking_label))} 예약</span>")

    mgr = str(manager or "").strip()
    det = str(details or "").strip()
    rows = [
        ("유형", f"<span class='rv-dot' style='background:{color}'></span>{icon} {esc(str(t_type))}"),
        ("시간", f"{esc(human_date(selected_date))} {esc(str(s_time))}~{esc(str(e_time))} "
                f"<span class='rv-sub'>· {duration_label(cur_minutes)}</span>"),
        ("운전자", esc(mgr) if mgr else "<span class='rv-sub'>—</span>"),
        ("상세", esc(det).replace("\n", "<br>") if det else "<span class='rv-sub'>—</span>"),
    ]
    grid = "".join(f"<div class='rv-l'>{l}</div><div class='rv-v'>{v}</div>" for l, v in rows)
    st.markdown(
        _RV_STYLE
        + f"<div class='rv-vno'>{esc(str(v_no))}</div>"
        + f"<div class='rv-chips'>{''.join(chips)}</div>"
        + f"<div class='rv-grid'>{grid}</div>",
        unsafe_allow_html=True,
    )

    if st.button("✏️ 수정하기", key=f"edit_btn_{res_id}", type="primary", use_container_width=True):
        _clear_edit_state(res_id)
        st.session_state.pop(f"confirm_delete_{res_id}", None)
        st.session_state[mode_key] = True
        st.rerun(scope="fragment")

    if is_dock:
        err_box = st.empty()
        label = "▶️ 부품대기 해제" if waiting else "⏸️ 부품대기로 전환"
        if st.button(label, key=f"toggle_wait_{res_id}", type="secondary", use_container_width=True):
            with st.spinner("저장 중…"):
                df_raw, df_day = load_day_df(selected_date)
                df = df_raw.dropna(subset=['id']).copy()
                idx = df.index[df['id'] == res_id].tolist()
                if not idx:
                    st.session_state["flash"] = {"msg": f"{v_no} 예약은 이미 삭제되어 있습니다.", "icon": "ℹ️"}
                    st.cache_data.clear()
                    st.rerun()
                if waiting:
                    # 해제하면 다시 도크를 차지 → 정원 재검증 (자기 자신 제외)
                    ok, reason = check_capacity(df_day, s_time, t_type, cur_minutes, exclude_id=res_id)
                    if not ok:
                        err_box.error(f"⛔ 부품대기를 해제할 수 없습니다. {reason}")
                        return
                if 'status' not in df.columns:
                    df['status'] = ""
                df['status'] = df['status'].fillna("").astype(str)
                df.at[idx[0], 'status'] = "" if waiting else STATUS_WAITING
                try:
                    conn.update(worksheet="Sheet1", data=df)
                except Exception as e:
                    sheet_failure("부품대기 상태를 저장", e)
            if waiting:
                finish_save(f"부품대기 해제: {v_no} · 작업 재개", "▶️", res_id)
            else:
                finish_save(f"부품대기로 전환: {v_no} · 도크 비움", "⏸️", res_id)


def _reservation_edit_form(res_id, v_no, manager, t_type, s_time, e_time, details,
                           status, cur_minutes, selected_date, mode_key):
    k_vno, k_mgr, k_type, k_start, k_details, k_wait = _edit_widget_keys(res_id)

    # 선택지에 (마감) 표시용: 이 날짜 예약 현황 (자기 자신은 check_capacity에서 제외)
    _, df_day = load_day_df(selected_date, ttl=30)

    st.caption(f"📅 {human_date(selected_date)} 예약 수정")
    new_v_no = st.text_input("차량 번호", value=v_no, key=k_vno)
    new_manager = st.text_input("운전자", value=manager, key=k_mgr)

    def minutes_for(task):
        durs = TASK_CONFIG[task]["durations"]
        return cur_minutes if cur_minutes in durs else durs[0]

    # 현재 선택값(위젯 상태) 기준으로 다른 선택지의 정원 여부 표시
    cur_start = st.session_state.get(k_start, s_time if s_time in slots else slots[0])
    cur_waiting = st.session_state.get(k_wait, status == STATUS_WAITING)

    def counts_toward(task):
        # 부품대기(도크)는 정원을 차지하지 않음
        return not (TASK_CONFIG[task]["pool"] == "dock" and cur_waiting)

    def full(task, start, minutes):
        if not counts_toward(task):
            return slot_range(start, minutes) is None
        ok, _ = check_capacity(df_day, start, task, minutes, exclude_id=res_id)
        return not ok

    type_options = [t for t in PRIMARY_TYPES if t in TASK_CATEGORIES] + \
                   [t for t in TASK_CATEGORIES if t not in PRIMARY_TYPES]
    new_task_type = st.pills(
        "정비 유형", type_options, key=k_type,
        default=t_type if t_type in type_options else None,
        format_func=lambda t: type_label(t) + (FULL_MARK if full(t, cur_start, minutes_for(t)) else ""),
    )

    new_details = details
    if new_task_type in DETAIL_TYPES:
        req = " (필수 기입)" if new_task_type in DETAIL_REQUIRED else ""
        new_details = st.text_area(f"📝 상세내용{req}", value=details if details else "", key=k_details)

    s_idx = slots.index(s_time) if s_time in slots else 0
    new_s_time = st.selectbox(
        "입고 시간", slots, index=s_idx, key=k_start,
        format_func=lambda s: s + (FULL_MARK if new_task_type
                                   and full(new_task_type, s, minutes_for(new_task_type)) else ""),
    )

    # 💡 소요시간 변경 = 조기 출고(줄이기) / 연장(늘리기)
    new_minutes, new_status = None, ""
    if new_task_type:
        new_minutes = duration_picker(
            "⏳ 소요시간 (조기출고·연장 시 변경)", new_task_type, new_s_time,
            is_full=lambda m: full(new_task_type, new_s_time, m),
            key=f"edit_dur_{res_id}_{new_task_type}",
            default_minutes=minutes_for(new_task_type),
        )
        if TASK_CONFIG[new_task_type]["pool"] == "dock":
            is_waiting = st.checkbox("⏸️ 부품대기 (도크 정원에서 제외, 정비고 내 대기)",
                                     value=(status == STATUS_WAITING), key=k_wait)
            new_status = STATUS_WAITING if is_waiting else ""

    new_e_time = None
    if new_minutes:
        occupied = slot_range(new_s_time, new_minutes)
        if occupied:
            new_e_time = end_time_of(occupied)
            st.caption(f"⏱️ 출고 예정 **{new_e_time}** ({duration_label(new_minutes)}, 점심시간 제외)")
        else:
            st.warning("운영시간(17:00)을 넘깁니다. 소요시간을 줄이거나 입고 시간을 앞당기세요.")

    err_box = st.empty()  # 오류는 저장 버튼 바로 위에 표시
    save_clicked = st.button("💾 저장", type="primary", use_container_width=True, key=f"save_edit_{res_id}")
    if st.button("수정 취소", key=f"cancel_edit_{res_id}", type="secondary", use_container_width=True):
        st.session_state.pop(mode_key, None)
        st.rerun(scope="fragment")

    # 오류 시 st.stop() 대신 return → 입력한 내용이 그대로 남음
    if save_clicked:
        new_v_no = normalize_vehicle_no(new_v_no)
        new_manager = str(new_manager or "").strip()
        err = validate_booking(new_v_no, new_manager, new_task_type, new_details, new_e_time,
                               selected_date=selected_date, start_time=new_s_time,
                               check_past=(new_s_time != s_time), minutes=new_minutes)
        if err:
            err_box.error(err)
            return

        with st.spinner("저장 중…"):
            df_raw, df_norm_day = load_day_df(selected_date)
            df = df_raw.dropna(subset=['id']).copy()
            if 'status' not in df.columns:
                df['status'] = ""

            # 💡 정원제: 수정 시에도 재검증하되, 자기 자신(res_id)은 카운트에서 제외
            #    부품대기 차량은 정원을 점유하지 않으므로 검증 생략
            if new_status != STATUS_WAITING:
                ok, reason = check_capacity(df_norm_day, new_s_time, new_task_type, new_minutes, exclude_id=res_id)
                if not ok:
                    err_box.error(f"⛔ {reason}")
                    return

            idx = df.index[df['id'] == res_id].tolist()
            if not idx:
                st.session_state.pop(mode_key, None)
                st.session_state["flash"] = {"msg": f"{v_no} 예약은 이미 삭제되어 있어 수정하지 못했습니다.", "icon": "ℹ️"}
                st.cache_data.clear()
                st.rerun()

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
            try:
                conn.update(worksheet="Sheet1", data=df)
            except Exception as e:
                sheet_failure("수정 내용을 저장", e)

        st.session_state.pop(mode_key, None)
        finish_save(f"수정됨: {booking_summary(new_v_no, new_task_type, new_s_time, new_e_time)}", "✏️", res_id)


def _delete_section(res_id, v_no, t_type, s_time, e_time, selected_date=None):
    # 🗑️ 삭제: 2단계 확인. 첫 탭은 확인창만 열고, '삭제' 확정을 눌러야 시트에서 지움 (되돌리기 없음)
    confirm_key = f"confirm_delete_{res_id}"
    if not st.session_state.get(confirm_key):
        if st.button("🗑️ 이 예약 삭제", key=f"ask_delete_{res_id}", type="tertiary", use_container_width=True):
            st.session_state[confirm_key] = True
            st.rerun(scope="fragment")
    else:
        when = f"{human_date(selected_date)} " if selected_date else ""
        st.warning(f"**{v_no}** · {t_type} · {when}{s_time}~{e_time} 예약을 삭제할까요?  \n"
                   "삭제하면 되돌릴 수 없습니다.")
        col_cancel, col_confirm = st.columns(2)
        with col_cancel:
            if st.button("취소", key=f"cancel_delete_{res_id}", type="secondary", use_container_width=True):
                st.session_state.pop(confirm_key, None)
                st.rerun(scope="fragment")
        with col_confirm:
            # 스타일 계약: 이 컨테이너 안의 primary 버튼은 빨간색(위험)으로 칠해짐
            with st.container(key="danger_delete"):
                do_delete = st.button("삭제", key=f"do_delete_{res_id}", type="primary", use_container_width=True)
        if do_delete:
            with st.spinner("삭제 중…"):
                try:
                    df = conn.read(worksheet="Sheet1", ttl=0)
                except Exception as e:
                    sheet_failure("최신 예약 현황을 불러오지", e)
                df = df.dropna(subset=['id'])

                if not (df['id'] == res_id).any():
                    # 다른 사람이 먼저 지웠거나 시트에서 직접 삭제된 경우
                    st.session_state.pop(confirm_key, None)
                    st.session_state["flash"] = {"msg": f"{v_no} 예약은 이미 삭제되어 있습니다.", "icon": "ℹ️"}
                    st.cache_data.clear()
                    st.rerun()

                updated_df = df[df['id'] != res_id]
                try:
                    conn.update(worksheet="Sheet1", data=updated_df)
                except Exception as e:
                    sheet_failure("예약을 삭제", e)

            st.session_state.pop(confirm_key, None)
            st.session_state.pop(f"edit_mode_{res_id}", None)
            st.session_state["flash"] = {"msg": f"삭제됨: {booking_summary(v_no, t_type, s_time, e_time)}", "icon": "🗑️"}
            st.cache_data.clear()
            time.sleep(SAVE_SETTLE_SEC)
            st.rerun()


# --- 🚀 예약 조회 / 수정 / 삭제 통합 모달창 ---
@st.dialog("📋 예약 상세 및 관리")
def reservation_modal(res_id, v_no, manager, t_type, s_time, e_time, details, status, selected_date, booking_label):
    mode_key = f"edit_mode_{res_id}"
    # 다른 예약을 새로 열었으면 항상 '보기' 모드로 시작
    if st.session_state.get("edit_open_for") != res_id:
        st.session_state.pop(mode_key, None)
        st.session_state["edit_open_for"] = res_id

    is_dock = TASK_CONFIG.get(t_type, {}).get("pool") == "dock"
    waiting = status == STATUS_WAITING
    cur_minutes = booking_minutes(s_time, e_time) if e_time else 30

    if not st.session_state.get(mode_key):
        _reservation_read_view(res_id, v_no, manager, t_type, s_time, e_time, details,
                               waiting, is_dock, cur_minutes, selected_date, booking_label, mode_key)
        # 삭제는 보기 화면에서만 (수정 중에는 저장/취소에 집중)
        st.divider()
        _delete_section(res_id, v_no, t_type, s_time, e_time, selected_date)
    else:
        _reservation_edit_form(res_id, v_no, manager, t_type, s_time, e_time, details,
                               status, cur_minutes, selected_date, mode_key)


# --- 📱 모바일 절대 방어 CSS ---
st.markdown("""
<style>
    .stApp { background-color: #f7f9fc; }
    /* 상단 여백 3.75rem = Streamlit 상단 메뉴바 높이 → 모바일에서 제목이 메뉴바에 가려지지 않도록.
       넓은 화면에서는 폰 레이아웃이 늘어나지 않도록 760px로 제한하고 가운데 정렬 */
    /* toolbarMode=minimal 이라 상단 메뉴바가 비어 있음 → 투명한 60px 띠를 없애고 고정 헤더를 맨 위에 붙임 */
    [data-testid="stHeader"] { display: none !important; }
    .block-container { padding: 0.75rem 0.5rem 2rem 0.5rem !important; max-width: 760px !important; margin-left: auto !important; margin-right: auto !important; }
    .main-title { font-size: 20px; font-weight: 800; color: #1a202c; text-align: center; margin-bottom: 10px; }
    /* Streamlit 영어 입력 안내("Press Ctrl+Enter to apply") 숨김 */
    [data-testid="InputInstructions"] { display: none !important; }

    @media (max-width: 640px) {
        /* 타임라인 3열: 시간 52px / 예약 1fr / ➕ 48px.
           Streamlit 1.54 열은 data-testid="stColumn"이고 자체 flex·width(calc %)를 가져 그리드 칸을 줄여버리므로 무력화 */
        div[data-testid="stHorizontalBlock"] { display: grid !important; grid-template-columns: 52px minmax(0, 1fr) 48px !important; gap: 6px !important; width: 100% !important; margin-bottom: 6px !important; }
        div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] { width: 100% !important; min-width: 0 !important; max-width: none !important; flex: none !important; }
    }
    /* 대화창 안의 2열(취소/삭제 등)은 폰에서도 반반 나란히 (세로로 쌓이지 않음) */
    div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] { display: flex !important; flex-direction: row !important; flex-wrap: nowrap !important; gap: 10px !important; }
    div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] { width: calc(50% - 5px) !important; flex: 1 1 0 !important; min-width: 0 !important; max-width: none !important; }

    /* 버튼 공통: 손가락 기준 최소 44px. :where()로 감싸 우선순위 0 → 개별 버튼 규칙(➕ 18px, ◀▶ 15px 등)이 이김 */
    :where(div[data-testid="stButton"] button) { border-radius: 6px !important; min-height: 44px !important; padding: 0 10px !important; width: 100% !important; overflow: hidden !important; }
    :where(div[data-testid="stButton"] button > div) { min-width: 0 !important; max-width: 100% !important; }
    :where(div[data-testid="stButton"] button p) { font-size: 14px !important; font-weight: 600 !important; margin: 0 !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; }
    /* 보조 버튼(secondary): 흰 바탕 + 테두리 #7b8798 (흰 3.65:1, 배경 #f7f9fc 3.46:1). 글자는 가운데 */
    button[kind="secondary"] { background-color: #ffffff !important; border: 1px solid #7b8798 !important; }
    button[kind="secondary"]:hover { border-color: #1F7A4D !important; background-color: #f4f8f5 !important; }
    button[kind="secondary"] p { color: #1a202c !important; }
    /* 예약 버튼만 왼쪽부터 시작하고 넘치면 오른쪽 말줄임 */
    [class*="st-key-view_"] div[data-testid="stButton"] button { justify-content: flex-start !important; text-align: left !important; }
    [class*="st-key-view_"] div[data-testid="stButton"] button > div { justify-content: flex-start !important; }
    [class*="st-key-view_"] div[data-testid="stButton"] button p { text-align: left !important; }
    /* 주요 버튼(primary): 테마 초록 + 흰 글자(5.3:1) */
    button[kind="primary"] { border: none !important; background-color: #1F7A4D !important; }
    button[kind="primary"]:hover, button[kind="primary"]:focus-visible { background-color: #18603D !important; }
    button[kind="primary"] p { color: #ffffff !important; }
    /* 삭제 확정 버튼: st.container(key="danger_delete") 안의 primary → 빨강(흰 글자 5.5:1) */
    .st-key-danger_delete button[kind="primary"] { background-color: #C53030 !important; }
    .st-key-danger_delete button[kind="primary"]:hover, .st-key-danger_delete button[kind="primary"]:focus-visible { background-color: #9B2C2C !important; }
    .st-key-danger_delete button[kind="primary"] p { color: #ffffff !important; }
    /* 텍스트 버튼(tertiary, 예: '수정 취소'): 중립 회색(흰 바탕 7.5:1). 빨강은 삭제 전용 */
    button[kind="tertiary"] p { color: #4a5568 !important; }
    button[kind="tertiary"]:hover p { color: #1a202c !important; text-decoration: underline; }
    /* '🗑️ 이 예약 삭제' 텍스트 버튼만 빨강(흰 바탕 6.6:1) */
    [class*="st-key-ask_delete_"] button p { color: #B42318 !important; }
    [class*="st-key-ask_delete_"] button:hover p { color: #9B2C2C !important; text-decoration: underline; }
    /* ➕ 추가 버튼: 16줄이 한꺼번에 튀지 않도록 흰 바탕 + 초록 테두리/글자 (kind와 무관하게 적용) */
    [class*="st-key-add_"] button { background-color: #ffffff !important; border: 1px solid #1F7A4D !important; color: #1F7A4D !important; padding: 0 !important; justify-content: center !important; }
    [class*="st-key-add_"] button > div { justify-content: center !important; }
    [class*="st-key-add_"] button p { color: #1F7A4D !important; font-size: 18px !important; font-weight: 700 !important; text-align: center !important; }
    [class*="st-key-add_"] button:hover { background-color: #e8f3ed !important; border-color: #18603D !important; }
    /* 🔒 마감 버튼(disabled)은 가운데 정렬 유지 */
    [class*="st-key-full_"] button { justify-content: center !important; padding: 0 !important; }
    [class*="st-key-full_"] button > div { justify-content: center !important; }
    .time-text { font-size: 14px; font-weight: 800; color: #4a5568; text-align: center; line-height: 44px; white-space: nowrap !important; margin: 0; padding: 0;}
    /* 빈 슬롯: 조용한 초록회색 글자, 대비 5.5:1 */
    .empty-slot { background-color: #f4f8f5; border-radius: 6px; text-align: center; color: #4D6B5B; font-size: 13px; font-weight: 600; line-height: 42px; border: 1px dashed #a9c2b4; margin: 0; height: 44px; box-sizing: border-box; }
    .full-slot { background-color: #fff5f5; border-radius: 6px; text-align: center; color: #c53030; font-size: 12px; font-weight: 700; line-height: 42px; border: 1px solid #feb2b2; margin: 0; height: 44px; box-sizing: border-box; }
    /* 펼침(범례 보기 등) 제목줄: 손가락 기준 44px, 글자 세로 가운데 */
    div[data-testid="stExpander"] summary { min-height: 44px !important; box-sizing: border-box !important; display: flex !important; align-items: center !important; padding-top: 0 !important; padding-bottom: 0 !important; }
    /* 📌 상단 고정 영역: 시계 + 날짜선택 + 범례 (Streamlit 메뉴바 3.75rem 바로 아래에 붙음) */
    /*    Streamlit이 컨테이너를 같은 높이의 stLayoutWrapper로 감싸므로 sticky는 그 래퍼에 적용해야 동작함.
          줄 배치(그리드 칸)는 GRID 영역의 .st-key-hdr_row 규칙이 담당 */
    div[data-testid="stLayoutWrapper"]:has(> .st-key-sticky_top) { position: sticky; top: 0; z-index: 990; }
    .st-key-sticky_top { background-color: #f7f9fc; padding: 4px 0 2px 0; gap: 6px !important; }
    .st-key-sticky_top div[data-testid="stColumn"] { width: 100% !important; min-width: 0 !important; }
    .st-key-sticky_top iframe { display: block; width: 100% !important; }
    .st-key-sticky_top div[data-testid="stPopover"], .st-key-sticky_top div[data-testid="stPopover"] > div { width: 100% !important; }
    div[data-testid="stPopover"] button { width: 100% !important; min-height: 44px !important; height: 44px; padding: 0 4px !important; border-radius: 8px !important; justify-content: center !important; }
    div[data-testid="stPopover"] button p { font-size: 15px !important; font-weight: 700 !important; white-space: nowrap !important; }
</style>
""", unsafe_allow_html=True)

import re as _re  # html 모듈은 DIALOGS 영역에서 _html 로 이미 import 됨

# 💡 유형 색상: TASK_CONFIG의 color 중 흰 배경 대비 3:1 미만인 것은 화면용으로만 진하게 보정
GRID_TYPE_COLORS = {
    "오일교환": "#B86E00",
    "요소수보충": "#1C7ED6",
    "정비점검": "#E03131",
    "경정비": "#2F855A",
}
POOL_LABELS = [("dock", "도크"), ("general", "일반"), ("mycar", "마이카")]


def grid_type_color(task_type):
    return GRID_TYPE_COLORS.get(task_type, TASK_CONFIG.get(task_type, {}).get("color", "#718096"))


def grid_key_class(key):
    """Streamlit이 위젯 key로 만드는 CSS 클래스명 (영문·숫자·_·- 외 문자는 '-'로 바뀜)."""
    return "st-key-" + _re.sub(r"[^a-zA-Z0-9_-]", "-", str(key))


def grid_id_str(v):
    """시트에서 1 / 1.0 / '1' 등으로 읽히는 id를 같은 문자열로 맞춤."""
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return str(v)


def grid_pool_usage(type_counts):
    """정원 풀별 (사용, 한도) — 비어 있지 않은 풀만. 예: [('도크', 1, 4)]"""
    out = []
    for pool, label in POOL_LABELS:
        types = [t for t, c in TASK_CONFIG.items() if c["pool"] == pool]
        used = sum(type_counts.get(t, 0) for t in types)
        if pool == "general":
            limit = SLOT_TOTAL_LIMIT
        else:
            limit = sum(TASK_CONFIG[t]["limit"] for t in types)
        if used:
            out.append((label, used, limit))
    return out


def grid_capacity_hints(type_counts):
    """예약 결정을 바꾸는 경우만 짧게: 풀이 꽉 찼으면 '일반 마감', 한 자리 남았으면 '도크 1자리 남음'."""
    hints = []
    for label, used, limit in grid_pool_usage(type_counts):
        left = limit - used
        if left <= 0:
            hints.append(f"{label} 마감")
        elif left == 1 and limit >= 3:
            hints.append(f"{label} 1자리 남음")
    return hints


def grid_now_slot(now_hm):
    """오늘 화면에서 스크롤해 보여줄 슬롯: 진행 중인 슬롯(점심시간이면 13:00). 운영 전/후면 None."""
    if now_hm < slots[0]:
        return None
    for s in slots:
        s_end = (datetime.strptime(s, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
        if now_hm < s_end:
            return s
    return None


def grid_shift_day(days):
    st.session_state["target_date"] = st.session_state.get("target_date", today_kst) + timedelta(days=days)


def grid_go_today():
    st.session_state["target_date"] = today_kst


st.markdown("""
<style>
    /* --- 상단 고정 영역: [시계/오늘로] [◀] [📅] [▶] 한 줄 --- */
    .st-key-sticky_top .st-key-hdr_row div[data-testid="stHorizontalBlock"] {
        display: grid !important; grid-template-columns: minmax(0, 1fr) 44px 76px 44px !important;
        gap: 6px !important; margin-bottom: 0 !important; align-items: center;
    }
    .st-key-hdr_row div[data-testid="stButton"] button,
    .st-key-hdr_row div[data-testid="stPopover"] button { min-height: 44px !important; height: 44px; }
    .st-key-prev_day button p, .st-key-next_day button p { font-size: 15px !important; }
    .st-key-go_today button { background: #FFF8E1 !important; border: 1px solid #E9C46A !important; }
    .st-key-go_today button p { color: #7A4E00 !important; font-size: 13px !important; font-weight: 700 !important; }
    .st-key-sticky_top div[data-testid="stExpander"] details { border-radius: 8px; background: #FFFFFF; }
    .st-key-sticky_top div[data-testid="stExpander"] summary { padding: 6px 12px; min-height: 36px; }
    .st-key-sticky_top div[data-testid="stExpander"] summary p { font-size: 13px; color: #4A5568; }
    .legend-line { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 13px; color: #2D3748; margin-bottom: 6px; }
    .legend-line span { white-space: nowrap; display: inline-flex; align-items: center; }
    .legend-note { font-size: 12.5px !important; color: #4A5568; line-height: 1.55; margin: 0; }

    /* --- 유형 색 칩 (10px 둥근 사각형) --- */
    .type-dot { display: inline-block; flex: 0 0 auto; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; }
    .type-dot.waiting { background: transparent !important; border: 2px solid currentColor; width: 10px; height: 10px; box-sizing: border-box; }
    /* 범례 견본: 그리드와 같은 부품대기 점선 칸 / 이어지는 시간 막대 */
    .legend-swatch { display: inline-block; flex: 0 0 auto; width: 22px; height: 12px; border-radius: 3px; margin-right: 6px; box-sizing: border-box; }
    .legend-swatch.waiting { border: 1px dashed #64748B; background: repeating-linear-gradient(135deg, #E2E8F0 0 3px, #F8FAFC 3px 6px); }
    [class*="st-key-view_"] button { justify-content: flex-start !important; padding-left: 10px !important; padding-right: 8px !important; }
    [class*="st-key-view_"] button::before {
        content: ""; flex: 0 0 auto; width: 10px; height: 10px; border-radius: 3px; margin-right: 8px; background: #718096;
    }

    /* --- 여러 슬롯에 걸친 예약의 이어지는 칸: 슬롯마다 한 줄로 '정비 중 N대 + 차량번호 ~출고' --- */
    .bay-line {
        display: flex; flex-wrap: wrap; align-items: center; gap: 4px 6px; margin: 0;
        padding: 5px 8px; border-radius: 6px; background: #EEF2F6; border: 1px solid #D5DDE6;
        font-size: 13px; line-height: 18px; color: #1A202C;
    }
    .bay-count { font-weight: 700; color: #334155; white-space: nowrap; margin-right: 2px; }
    .bay-car {
        display: inline-flex; align-items: center; white-space: nowrap; font-weight: 600;
        background: #FFFFFF; border: 1px solid #CBD5E0; border-radius: 999px; padding: 1px 8px 1px 6px;
    }
    .bay-car .type-dot { width: 9px; height: 9px; margin-right: 5px; }
    .bay-car .bay-end { font-weight: 500; color: #475569; margin-left: 4px; }
    .bay-car.waiting { background: #F8FAFC; border: 1px dashed #64748B; color: #4A5568; font-weight: 500; }

    /* --- 예약이 있어도 남은 자리가 있으면 계속 보이는 '예약 가능' 줄 --- */
    .avail-line { font-size: 13px; line-height: 18px; margin: 0; padding: 0 4px; color: #1F7A4D; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }   /* #f7f9fc 위 5.0:1 */
    .avail-line .avail-tight { color: #9A3412; }

    /* --- 정원 안내 --- */
    [class*="st-key-row_"] div[data-testid="stColumn"] div[data-testid="stVerticalBlock"] { gap: 4px !important; }
    [class*="st-key-row_"] div[data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
    [class*="st-key-row_"] { border-bottom: 1px solid #E2E8F0; padding-bottom: 6px; border-radius: 8px; }
    [class*="st-key-view_"] button > div, [class*="st-key-view_"] button p { text-align: left !important; justify-content: flex-start !important; }
    .cap-hint { font-size: 12.5px !important; color: #475569; margin: 0; padding: 0 4px; line-height: 18px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .cap-hint.tight { color: #9A3412; font-weight: 700; }   /* 7:1 이상 */
    .empty-slot .cap-sub { font-weight: 500; color: #475569; }

    /* --- 지난 시간 / 지금 --- */
    .time-text.time-past { color: #64748B; font-weight: 600; }   /* #f7f9fc 위 4.5:1, 기본 #4a5568보다 옅음 */
    .empty-slot.slot-past { background: #F1F5F9; color: #5B6576; border-style: solid; border-color: #E2E8F0; }
    .time-text.time-now {
        display: flex; flex-direction: column; justify-content: center; align-items: center;
        line-height: 1.1 !important; height: 44px; color: #1F7A4D;
    }
    .now-pill { display: inline-block; margin-top: 2px; padding: 1px 6px; border-radius: 999px; background: #1F7A4D; color: #FFFFFF; font-size: 12px; font-weight: 700; line-height: 15px; }

    /* --- 점심시간 구분선 --- */
    .lunch-break { display: flex; align-items: center; gap: 8px; margin: 2px 0 8px 0; color: #5B6576; font-size: 12px; font-weight: 600; }
    .lunch-break::before, .lunch-break::after { content: ""; flex: 1; border-top: 1px solid #CBD5E0; }

    /* --- 방금 저장한 예약 1회 강조 --- */
    @keyframes grid-hl-fade {
        0%, 40% { box-shadow: 0 0 0 3px #1F7A4D; background-color: #E6F4EC; }
        100% { box-shadow: 0 0 0 0 rgba(31, 122, 77, 0); }
    }
    .bay-line.hl-flash { animation: grid-hl-fade 2.6s ease-out 1; }

    /* --- 지난 시간 ＋ 버튼: 비활성 + 흐리게 --- */
    [class*="st-key-add_"] button:disabled { background-color: #F1F5F9 !important; border-color: #CBD5E0 !important; cursor: not-allowed; }
    [class*="st-key-add_"] button:disabled p { color: #94A3B8 !important; }

    /* --- 직전 동작 결과 배너 (상단 고정 영역 바로 아래, 스크롤과 함께 흐름) --- */
    @keyframes grid-flash-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
    .flash-banner {
        display: flex; align-items: center; gap: 8px; margin: 2px 0 4px 0; padding: 10px 12px;
        border-radius: 8px; font-size: 14px; font-weight: 700; line-height: 1.4;
        background: #E6F4EC; border: 1px solid #9FD0B4; border-left: 4px solid #1F7A4D; color: #14532D;
        animation: grid-flash-in .25s ease-out 1;
    }
    .flash-banner.del { background: #FDF2F2; border-color: #F5C2C2; border-left-color: #C53030; color: #7F1D1D; }
    .flash-banner .flash-icon { flex: 0 0 auto; font-size: 16px; }

    /* --- 날짜 요약 한 줄 (스크롤 영역) --- */
    .day-summary { font-size: 13px; color: #4A5568; font-weight: 600; text-align: center; margin: 0 0 4px 0; }
    .day-summary b { color: #1A202C; }

    @media (prefers-reduced-motion: reduce) {
        .bay-line.hl-flash { animation: none; box-shadow: 0 0 0 2px #1F7A4D; }
        [class*="st-key-row_"] { animation: none !important; }
        .flash-banner { animation: none; }
    }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">🔧 정비고 입출고 관리</div>', unsafe_allow_html=True)

# 💡 상단 고정 영역 (시계 + ◀ 📅 ▶ + 범례) — CSS .st-key-sticky_top 으로 sticky 처리
today_kst = datetime.now(KST).date()
WEEKDAYS = "월화수목금토일"

# 조회 날짜는 date_input 위젯 key("target_date")에 보관. ◀/▶/오늘로는 on_click 콜백에서만 바꿈
if "target_date" not in st.session_state:
    st.session_state["target_date"] = today_kst

with st.container(key="sticky_top"):
    with st.container(key="hdr_row"):
        col_clock, col_prev, col_cal, col_next = st.columns([6, 1, 1.6, 1], vertical_alignment="center")

    sel = st.session_state.get("target_date") or today_kst
    with col_clock:
        if sel == today_kst:
            # 현재시간 시계: 브라우저에서 15초마다 갱신 (앱 재실행 없이), 항상 한국시간 기준
            components.html("""
<style>html, body { margin: 0; padding: 0; overflow: hidden; background: transparent; }</style>
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#FFFFFF;color:#1A202C;border:1px solid #E2E8F0;border-radius:8px;height:44px;box-sizing:border-box;
     display:flex;justify-content:center;align-items:center;gap:8px;">
  <span id="clock-date" style="font-size:13px;color:#4A5568;font-weight:600;"></span>
  <span id="clock-time" style="font-size:18px;font-weight:800;font-variant-numeric:tabular-nums;"></span>
</div>
<script>
  const dFmt = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul', month:'long', day:'numeric', weekday:'short'});
  const tFmt = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul', hour:'2-digit', minute:'2-digit', hour12:false});
  function tick() {
    const now = new Date();
    document.getElementById('clock-date').textContent = dFmt.format(now);
    document.getElementById('clock-time').textContent = tFmt.format(now);
  }
  tick(); setInterval(tick, 15000);
</script>
""", height=44)
        else:
            # 다른 날짜를 보는 중 → 시계 자리에 '오늘로' 버튼 (헤더 높이 유지)
            st.button(f"📌 {sel.month}/{sel.day}({WEEKDAYS[sel.weekday()]}) 보는 중 · 오늘로",
                      key="go_today", on_click=grid_go_today, use_container_width=True)

    with col_prev:
        st.button("◀", key="prev_day", help="전날", on_click=grid_shift_day, args=(-1,), use_container_width=True)
    with col_cal:
        # 📅 아이콘을 누르면 달력이 열림. 오늘이 아닌 날짜를 보고 있으면 아이콘 옆에 그 날짜를 표시
        cal_label = "📅" if sel == today_kst else f"📅{sel.month}/{sel.day}"
        with st.popover(cal_label, use_container_width=True):
            target_date = st.date_input("조회 날짜", key="target_date")
    with col_next:
        st.button("▶", key="next_day", help="다음날", on_click=grid_shift_day, args=(1,), use_container_width=True)

    with st.expander("범례 보기"):
        legend_html = "".join(
            f"<span><i class='type-dot' style='background:{grid_type_color(k)}'></i>{k}</span>"
            for k in TASK_CONFIG
        )
        legend_html += ("<span><i class='legend-swatch waiting'></i>⏸️ 부품대기</span>"
                        "<span>🔧 정비 중 N대: 이전 시간에 들어와 아직 정비고에 있는 차량</span>"
                        "<span>📅 미리 잡은 예약</span><span>⚡ 당일 접수</span>")
        st.markdown(f"<div class='legend-line'>{legend_html}</div>", unsafe_allow_html=True)
        dock_n = TASK_CONFIG["차량정비"]["limit"]
        mycar_n = TASK_CONFIG["마이카"]["limit"]
        st.markdown(
            "<div class='legend-note'>30분마다 받을 수 있는 차량: "
            f"차량정비는 정비도크 {dock_n}대, 마이카 {mycar_n}대, "
            f"그 밖의 작업(오일·요소수·점검·타이어·경정비·기타)은 합쳐서 {SLOT_TOTAL_LIMIT}대까지.<br>"
            "부품대기 차량은 자리를 차지하지 않습니다.</div>",
            unsafe_allow_html=True,
        )

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
    # 💡 읽기 실패 시 빈 그리드를 그리면 '전부 예약 가능'으로 보여 중복 예약을 부름 → 그리드를 숨기고 멈춤
    st.error("**예약 현황을 불러오지 못했습니다.**  \n"
             "지금 보이는 화면으로는 빈 시간을 알 수 없으니 예약하지 마세요. "
             "인터넷 연결을 확인한 뒤 다시 시도해주세요.")
    st.button("🔄 다시 불러오기", type="primary", use_container_width=True)
    with st.expander("기술 정보 (관리자용)"):
        st.code(f"{type(e).__name__}: {e}")
    st.stop()

# 💡 직전 동작 결과 배너: 상단 고정 영역 바로 아래(스크롤 영역)에 한 번만 표시 — 다음 재실행 때 사라짐
flash = st.session_state.pop("flash", None)
if flash:
    if isinstance(flash, dict):
        f_msg, f_icon = str(flash.get("msg", "")), str(flash.get("icon") or "✅")
    else:
        f_msg, f_icon = str(flash), "✅"
    is_del = f_icon == "🗑️" or "삭제" in f_msg
    st.markdown(f"<div class='flash-banner{' del' if is_del else ''}' role='status' aria-live='polite'>"
                f"<span class='flash-icon' aria-hidden='true'>{_html.escape(f_icon)}</span>"
                f"<span>{_html.escape(f_msg)}</span></div>", unsafe_allow_html=True)

# 💡 날짜 요약 한 줄 (고정 영역 높이를 늘리지 않도록 스크롤 영역에 둠)
_day_txt = f"{target_date.month}/{target_date.day}({WEEKDAYS[target_date.weekday()]})"
if df_res.empty:
    st.markdown(f"<div class='day-summary'>{_day_txt} 예약 <b>0건</b> — 모든 시간 예약 가능</div>",
                unsafe_allow_html=True)
else:
    _n_wait = int((df_res['status'] == STATUS_WAITING).sum()) if 'status' in df_res.columns else 0
    _sum = f"{_day_txt} 예약 <b>{len(df_res)}건</b>"
    if _n_wait:
        _sum += f" · 부품대기 <b>{_n_wait}대</b>"
    st.markdown(f"<div class='day-summary'>{_sum}</div>", unsafe_allow_html=True)

# --- 타임라인 렌더링 ---
# 💡 방금 등록/수정한 예약 id (대화상자 쪽에서 설정) → 이번 한 번만 강조하고 지움
highlight_id = st.session_state.pop("highlight_id", None)
highlight_id = grid_id_str(highlight_id) if highlight_id is not None else None

is_today = target_date == today_kst
now_hm = datetime.now(KST).strftime("%H:%M")
per_booking_css = []

for slot in slots:
    slot_end = (datetime.strptime(slot, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
    is_past = is_today and slot_end <= now_hm
    is_now = is_today and slot <= now_hm < slot_end

    if df_res.empty:
        active = pd.DataFrame()
    else:
        active = df_res[(df_res['start_time'] <= slot) & (df_res['end_time'] > slot)]
    # 💡 여러 슬롯에 걸친 예약은 첫 점유 슬롯(입고 시간)에만 버튼, 나머지 슬롯엔 '이어지는 줄'로 표시
    if not active.empty:
        starts_here = active.apply(lambda r: first_slot_of(r['start_time'], r['end_time']) == slot, axis=1)
        current_tasks = active[starts_here]
        continuing = active[~starts_here]
    else:
        current_tasks = continuing = active

    # 💡 정원제: 어떤 유형도 더 받을 수 없을 때만 마감 처리
    slot_counts, slot_general = count_slot(df_res, slot)
    is_full = not any(slot_has_room(slot_counts, slot_general, t) for t in TASK_CATEGORIES)
    cap_hints = grid_capacity_hints(slot_counts)

    row_box = st.container(key=f"row_{slot.replace(':', '')}")
    col_time, col_tasks, col_add = row_box.columns([1.5, 7, 1.5])

    with col_time:
        if is_now:
            st.markdown(f"<div class='time-text time-now'>{slot}<span class='now-pill'>지금</span></div>",
                        unsafe_allow_html=True)
        else:
            st.markdown(f"<div class='time-text{' time-past' if is_past else ''}'>{slot}</div>",
                        unsafe_allow_html=True)

    with col_tasks:
        # 💡 이전 슬롯에서 들어와 아직 정비고에 있는 차량: 슬롯마다 한 줄로 대수 + 차량번호 + 출고 예정
        if not continuing.empty:
            n_work = int((continuing['status'] != STATUS_WAITING).sum())
            n_wait = len(continuing) - n_work
            count_txt = " · ".join(
                ([f"🔧 정비 중 {n_work}대"] if n_work else []) + ([f"⏸️ 부품대기 {n_wait}대"] if n_wait else [])
            )
            chips, hl = [], False
            for _, row in continuing.iterrows():
                waiting = row.get('status') == STATUS_WAITING
                hl = hl or grid_id_str(row['id']) == highlight_id
                color = grid_type_color(row['task_type'])
                dot_style = (f"color:{color}" if waiting else f"background:{color}")
                chips.append(
                    f"<span class='bay-car{' waiting' if waiting else ''}' "
                    f"title='{_html.escape(str(row['task_type']), quote=True)}'>"
                    f"<i class='type-dot{' waiting' if waiting else ''}' style='{dot_style}'></i>"
                    f"{_html.escape(str(row['vehicle_no']))}"
                    f"<span class='bay-end'>~{_html.escape(str(row['end_time']))}</span></span>"
                )
            st.markdown(f"<div class='bay-line{' hl-flash' if hl else ''}'>"
                        f"<span class='bay-count'>{count_txt}</span>{''.join(chips)}</div>",
                        unsafe_allow_html=True)

        for _, row in current_tasks.iterrows():
            # 💡 사전/당일 뱃지 판별
            booking_label, badge = classify_booking(row.get('created_at'), row['date'])
            waiting = row.get('status') == STATUS_WAITING
            # 라벨: 차량번호 먼저 → 유형 → (1시간 이상이면) 시간 범위 → 뱃지 1개 (부품대기 우선)
            btn_label = f"{row['vehicle_no']} · {row['task_type']}"
            if booking_minutes(row['start_time'], row['end_time']) > 30:
                btn_label += f" {row['start_time']}~{row['end_time']}"
            mark = "⏸️" if waiting else badge
            if mark:
                btn_label += f" {mark}"

            btn_key = f"view_{row['id']}_{slot}"
            kc = "." + grid_key_class(btn_key)
            color = grid_type_color(row['task_type'])
            if waiting:
                per_booking_css.append(
                    f"{kc} button {{ border: 1px dashed #94A3B8 !important; background: #F8FAFC !important; }}"
                    f"{kc} button p {{ color: #4A5568 !important; font-weight: 500 !important; }}"
                    f"{kc} button::before {{ background: transparent; border: 2px solid {color}; box-sizing: border-box; }}"
                )
            else:
                per_booking_css.append(f"{kc} button::before {{ background: {color}; }}")
            if grid_id_str(row['id']) == highlight_id:
                rc = "." + grid_key_class(f"row_{slot.replace(':', '')}")
                per_booking_css.append(f"{rc} {{ animation: grid-hl-fade 2.6s ease-out 1; }}")
                per_booking_css.append(
                    f"{kc} button {{ animation: grid-hl-fade 2.6s ease-out 1; }}"
                    f"@media (prefers-reduced-motion: reduce) {{ {kc} button {{ animation: none; "
                    f"box-shadow: 0 0 0 2px #1F7A4D; }} }}"
                )

            if st.button(btn_label, key=btn_key, use_container_width=True):
                # 이전에 열다 닫은 삭제 확인창/수정 모드가 남아 있지 않도록 초기화
                st.session_state.pop(f"confirm_delete_{row['id']}", None)
                st.session_state.pop(f"edit_mode_{row['id']}", None)
                safe_details = str(row['details']) if pd.notna(row.get('details')) else ""
                reservation_modal(row['id'], row['vehicle_no'], row['manager'], row['task_type'],
                                  row['start_time'], row['end_time'], safe_details,
                                  row.get('status', ""), target_date, booking_label)

        has_items = not active.empty
        if not has_items:
            if is_full:
                st.markdown("<div class='full-slot'>마감</div>", unsafe_allow_html=True)
            elif is_past:
                st.markdown("<div class='empty-slot slot-past'>지난 시간</div>", unsafe_allow_html=True)
            else:
                st.markdown("<div class='empty-slot'>예약 가능</div>", unsafe_allow_html=True)
        elif is_full:
            st.markdown("<div class='cap-hint tight'>마감 · 더 받을 수 없음</div>", unsafe_allow_html=True)
        elif not is_past:
            # 💡 예약이 1대라도 있으면 '예약 가능'이 사라져 마감으로 착각하던 문제 → 남은 자리가 있으면 항상 표시
            #    일부 풀이 마감이면 '예약 가능 · 일반 마감'처럼 섞어 쓰지 않고, 아직 받을 수 있는 작업만 안내
            open_pools = []
            for t in TASK_CATEGORIES:
                p = TASK_CONFIG[t]["pool"]
                if p not in open_pools and slot_has_room(slot_counts, slot_general, t):
                    open_pools.append(p)
            all_pools = {v["pool"] for v in TASK_CONFIG.values()}
            if set(open_pools) == all_pools:
                avail_txt = "✓ 예약 가능"
            else:
                pool_names = {"dock": "차량정비", "mycar": "마이카", "general": "일반 작업"}
                avail_txt = "✓ " + "·".join(pool_names.get(p, p) for p in open_pools) + "만 예약 가능"
            left_hints = [h for h in cap_hints if h.endswith("남음")]
            tight = f" <span class='avail-tight'>· {' · '.join(left_hints)}</span>" if left_hints else ""
            st.markdown(f"<div class='avail-line'>{avail_txt}{tight}</div>", unsafe_allow_html=True)

    with col_add:
        if is_full:
            # 💡 정원제: 총량 마감 시 신규 예약 차단
            st.button("🔒", key=f"full_{slot}", disabled=True, help="이 시간은 마감", use_container_width=True)
        else:
            # "+" 단독은 마크다운 목록 기호로 해석돼 빈 버튼이 됨 → 이스케이프
            if st.button("\\+", key=f"add_{slot}", type="secondary", help="예약 추가",
                         disabled=is_past_slot(target_date, slot), use_container_width=True):
                create_reservation_modal(target_date, slot, df_res)

    if slot == "11:30":
        st.markdown("<div class='lunch-break'>점심시간 12:00–13:00</div>", unsafe_allow_html=True)

# 예약별 색 칩 / 부품대기 / 방금 저장 강조 스타일 (버튼은 HTML을 못 담으므로 key 클래스로 지정)
if per_booking_css:
    st.markdown("<style>" + "".join(per_booking_css) + "</style>", unsafe_allow_html=True)

# 💡 오늘 화면을 처음 열었을 때만 현재 슬롯이 고정 헤더 바로 아래 오도록 스크롤 (저장 후 재실행에선 안 함)
_prev_view = st.session_state.get("grid_last_view")
st.session_state["grid_last_view"] = str(target_date)
_jump_slot = grid_now_slot(now_hm) if is_today else None
if _jump_slot and _prev_view != str(target_date):
    components.html("""
<script>
(function () {
  try {
    const doc = window.parent.document;
    const mq = window.parent.matchMedia && window.parent.matchMedia('(prefers-reduced-motion: reduce)');
    const reduce = !!(mq && mq.matches);
    let tries = 0;
    function go() {
      const el = doc.querySelector('.st-key-row___SLOT__');
      if (!el) { if (++tries < 20) setTimeout(go, 150); return; }
      const top = doc.querySelector('.st-key-sticky_top');
      const off = top ? Math.max(0, top.getBoundingClientRect().bottom) + 8 : 120;
      el.style.scrollMarginTop = off + 'px';
      el.scrollIntoView({block: 'start', behavior: reduce ? 'auto' : 'smooth'});
    }
    setTimeout(go, 250);
  } catch (e) { /* 다른 출처 프레임이면 조용히 건너뜀 */ }
})();
</script>
""".replace("__SLOT__", _jump_slot.replace(":", "")), height=0)
