"""In-memory thread-safe state container and tracker for NEXXA robot telemetry.

Tracks live state (idle, moving, inspecting, analyzing, summarizing, returning, docked, error),
inspection step pipeline (1..5), battery, Wi-Fi RSSI, the latest camera frame (relay), and handles
automatic synchronization from round creation and leaf capture analysis.

The relayed camera frame lives in memory only (latest JPEG + receive time): it is lost on restart and not
shared between worker processes (shortcut: single process; use a shared store if scaled out).
"""
import math
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Station

STATE_LABELS = {
    "idle": "พร้อมเริ่มตรวจรอบใหม่",
    "moving": "กำลังเคลื่อนที่",
    "inspecting": "กำลังตรวจสอบ",
    "analyzing": "กำลังวิเคราะห์ผล",
    "summarizing": "กำลังสรุปผล",
    "returning": "กำลังเดินทางกลับจุดจอด",
    "docked": "จอดที่แท่นชาร์จ",
    "error": "เกิดข้อผิดพลาด",
    "offline": "ออฟไลน์",
}

ANGLE_LABELS = {
    "front": "หน้า",
    "right": "ขวา",
    "back": "หลัง",
    "left": "ซ้าย",
}

VALID_STATES = {"idle", "moving", "inspecting", "analyzing", "summarizing", "returning", "docked", "error"}
VALID_ANGLES = {"front", "right", "back", "left"}

FRAME_MIN_INTERVAL_S = 1  # minimum seconds between accepted frames
FRAME_MAX_AGE_S = 60  # an older frame is not served and the camera counts as unavailable
FRAME_REFRESH_S = 5  # how often the dashboard re-fetches while the robot is online


class RobotMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self.state: str = "idle"
        self.round_id: int | None = None
        self.station_id: int | None = None
        self.station_name: str | None = None
        self.camera_angle: str | None = None
        self.step: str = "พร้อมเริ่มรอบใหม่"
        self.step_index: int = 1
        self.total_steps: int = 5
        self.battery_pct: int | None = None
        self.wifi_rssi: int | None = None
        self.updated_at: float = 0.0
        self._frame: bytes | None = None
        self._frame_at: float = 0.0

    def reset(self) -> None:
        """Reset state to initial defaults (used for testing and full reset)."""
        with self._lock:
            self.state = "idle"
            self.round_id = None
            self.station_id = None
            self.station_name = None
            self.camera_angle = None
            self.step = "พร้อมเริ่มรอบใหม่"
            self.step_index = 1
            self.total_steps = 5
            self.battery_pct = None
            self.wifi_rssi = None
            self.updated_at = 0.0
            self._frame = None
            self._frame_at = 0.0

    def update_telemetry(self, session: Session | None, data: dict) -> None:
        """Update robot monitor from external telemetry JSON payload."""
        with self._lock:
            if "state" in data and data["state"] in VALID_STATES:
                self.state = data["state"]
            if "round_id" in data:
                self.round_id = data["round_id"]
            if "camera_angle" in data:
                ang = data["camera_angle"]
                self.camera_angle = ang if ang in VALID_ANGLES else None
            if "step" in data and isinstance(data["step"], str):
                self.step = data["step"]
            if "step_index" in data and data["step_index"] is not None:
                try:
                    idx = int(data["step_index"])
                    if 1 <= idx <= self.total_steps:
                        self.step_index = idx
                except (ValueError, TypeError):
                    pass
            if "battery_pct" in data and data["battery_pct"] is not None:
                try:
                    pct = int(data["battery_pct"])
                    self.battery_pct = max(0, min(100, pct))
                except (ValueError, TypeError):
                    pass
            if "wifi_rssi" in data and data["wifi_rssi"] is not None:
                try:
                    self.wifi_rssi = int(data["wifi_rssi"])
                except (ValueError, TypeError):
                    pass

            st_id = data.get("station_id")
            rfid = data.get("rfid_uid")
            if session:
                try:
                    if rfid and not st_id:
                        station = session.scalar(select(Station).where(Station.rfid_uid == rfid))
                        if station:
                            self.station_id = station.id
                            self.station_name = station.name
                    elif st_id is not None:
                        self.station_id = st_id
                        station = session.get(Station, st_id)
                        if station:
                            self.station_name = station.name
                except Exception:
                    pass
            elif st_id is not None:
                self.station_id = st_id

            self.updated_at = time.time()

    def frame_retry_after(self) -> int:
        """Seconds the caller must still wait before a new frame is accepted (0 = now)."""
        with self._lock:
            wait = FRAME_MIN_INTERVAL_S - (time.time() - self._frame_at)
        return max(0, math.ceil(wait))

    def store_frame(self, data: bytes) -> int:
        """Keep `data` as the latest frame and count it as a heartbeat. Returns 0, or the retry-after seconds."""
        with self._lock:
            now = time.time()
            wait = FRAME_MIN_INTERVAL_S - (now - self._frame_at)
            if wait > 0:
                return max(1, math.ceil(wait))
            self._frame, self._frame_at, self.updated_at = data, now, now
            return 0

    def get_frame(self) -> tuple[bytes, int] | None:
        """(jpeg, age in whole seconds) of the latest frame, or None when there is none or it is too old."""
        with self._lock:
            if self._frame is None:
                return None
            age = int(time.time() - self._frame_at)
            return (self._frame, age) if age <= FRAME_MAX_AGE_S else None

    def update_from_round(self, round_id: int) -> None:
        """Triggered automatically when a new inspection round is created."""
        with self._lock:
            self.round_id = round_id
            self.state = "moving"
            self.station_id = None
            self.station_name = None
            self.camera_angle = None
            self.step = "เริ่มรอบตรวจใหม่ เดินทางไปยังสถานีแรก"
            self.step_index = 2
            self.updated_at = time.time()

    def update_from_capture(
        self,
        session: Session | None,
        round_id: int,
        station_id: int,
        camera_angle: str | None,
        quality_ok: bool | None,
        status: str,
        attempt: int,
    ) -> None:
        """Triggered automatically when a camera capture is submitted and analyzed."""
        with self._lock:
            self.round_id = round_id
            self.station_id = station_id
            self.camera_angle = camera_angle if camera_angle in VALID_ANGLES else camera_angle

            st_name = None
            if session:
                try:
                    st = session.get(Station, station_id)
                    if st:
                        st_name = st.name
                        self.station_name = st.name
                except Exception:
                    pass
            st_display = st_name or self.station_name or f"สถานี {station_id}"
            angle_th = ANGLE_LABELS.get(camera_angle or "", camera_angle or "")
            angle_disp = f"มุม{angle_th}" if angle_th else ""

            if status == "retake" or quality_ok is False:
                self.state = "inspecting"
                self.step_index = 3
                self.step = f"{st_display} {angle_disp} ภาพไม่ชัด กำลังถ่ายซ้ำ (ครั้งที่ {attempt})".strip()
            else:
                self.state = "analyzing"
                self.step_index = 4
                self.step = f"{st_display} {angle_disp} AI กำลังวิเคราะห์ผล".strip()

            self.updated_at = time.time()

    def get_robot_status(self, session: Session | None = None) -> dict:
        """Return public read-only status dict for the dashboard and public API."""
        with self._lock:
            now = time.time()
            is_online = (self.updated_at > 0) and ((now - self.updated_at) <= 45)

            station_name = self.station_name
            if not station_name and self.station_id and session:
                try:
                    st = session.get(Station, self.station_id)
                    if st:
                        station_name = st.name
                except Exception:
                    pass

            frame_age = int(now - self._frame_at) if self._frame is not None else None

            raw_state = self.state if is_online else "offline"
            state_label = STATE_LABELS.get(raw_state, "ออฟไลน์" if not is_online else raw_state)
            angle_label = ANGLE_LABELS.get(self.camera_angle or "", self.camera_angle) if self.camera_angle else None

            step_desc = self.step if is_online else "หุ่นยนต์ออฟไลน์ (ขาดการติดต่อเกิน 45 วินาที)"

            return {
                "online": is_online,
                "state": raw_state,
                "state_label": state_label,
                "round_id": self.round_id,
                "station_id": self.station_id,
                "station_name": station_name,
                "camera_angle": self.camera_angle,
                "camera_angle_label": angle_label,
                "step": step_desc,
                "step_index": self.step_index if is_online else 1,
                "total_steps": self.total_steps,
                "camera": {
                    "available": frame_age is not None and frame_age <= FRAME_MAX_AGE_S,
                    "age_s": frame_age,
                    "refresh_s": FRAME_REFRESH_S,
                },
                "battery_pct": self.battery_pct if is_online else None,
                "wifi_rssi": self.wifi_rssi if is_online else None,
                "updated_at": self.updated_at,
            }


robot = RobotMonitor()
