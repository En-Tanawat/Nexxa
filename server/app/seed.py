from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Crop, Station

TIPBURN_HINT = (
    "ขอบใบสีน้ำตาลที่ใบอ่อน/ใบด้านใน โดยไม่มีแผลลุกลาม น่าจะ tipburn "
    "ให้ตอบ จุดน้ำตาล พร้อมหมายเหตุว่า \"น่าจะ tipburn\" "
    "ส่วนใบแก่ด้านนอกที่เหลืองตามธรรมชาติให้ตอบ ปกติ"
)

CROP_NAME = "กรีนคอส"
LABELS = ["ปกติ", "ใบเหลือง", "จุดน้ำตาล"]
PROMPT = (
    "ดูภาพถ่ายใบผักสลัดกรีนคอส (green cos) แล้วเลือกป้ายกำกับเพียงหนึ่งอย่างจากรายการของพืชนี้ "
    "ได้แก่ " + ", ".join(LABELS) + "."  # TIPBURN_HINT goes in crop.notes; build_prompt includes both
)

# TODO: replace the placeholder rfid_uid values with the real tag UIDs when the tags are installed.
STATIONS = [
    dict(rfid_uid="TODO-REPLACE-1", name="สถานี 1", kind="station", route_order=1, map_x=1.0, map_y=0.0),
    dict(rfid_uid="TODO-REPLACE-2", name="สถานี 2", kind="station", route_order=2, map_x=2.0, map_y=0.0),
    dict(rfid_uid="TODO-REPLACE-3", name="สถานี 3", kind="station", route_order=3, map_x=3.0, map_y=0.0),
    dict(rfid_uid="TODO-REPLACE-HOME", name="จุดเริ่มต้น", kind="home", route_order=0, map_x=0.0, map_y=0.0),
]


def seed(session: Session) -> None:
    crop = session.scalar(select(Crop).where(Crop.name == CROP_NAME))
    if crop is None:
        crop = Crop(name=CROP_NAME, labels=LABELS, prompt=PROMPT, notes=TIPBURN_HINT)
        session.add(crop)
        session.flush()
    # Seed stations only into an empty table: real UIDs replace the placeholders later.
    if session.scalar(select(Station.id).limit(1)) is None:
        for s in STATIONS:
            session.add(Station(**s, crop_id=None if s["kind"] == "home" else crop.id))
    session.commit()
