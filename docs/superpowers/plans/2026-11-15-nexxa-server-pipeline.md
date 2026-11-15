# NEXXA Server Pipeline (ขั้น 1–2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Server ที่รับภาพ 1 สถานี → กรองคุณภาพด้วย OpenCV → ให้ Claude แยก ปกติ/ใบเหลือง/จุดน้ำตาล → บันทึกผล พร้อมชุดทดสอบ ~30 ภาพจากเน็ตที่วัดความแม่นยำได้

**Architecture:** FastAPI + SQLAlchemy (SQLite ตอนพัฒนา/ทดสอบ, Postgres ตอนขึ้น hosting ผ่าน `DATABASE_URL` เดียวกัน) โมดูลเล็กแยกหน้าที่: `quality.py` (OpenCV), `analyzer.py` (Claude), `main.py` (HTTP) ภาพเก็บในโฟลเดอร์ผ่านฟังก์ชันเดียว ย้ายไป object storage ทีหลังได้

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2, opencv-python-headless, numpy, anthropic SDK, pytest, httpx (TestClient)

**Spec:** `docs/2026-11-15-nexxa-design.md` (หัวข้อ 4–8)

## Global Constraints

- ตรวจ 2 อาการเท่านั้น: label ที่ยอมรับ = `ปกติ`, `ใบเหลือง`, `จุดน้ำตาล` (ค่าอยู่ในตาราง `crops.labels` ไม่ฝังในโค้ด)
- ชนิดผักเริ่มที่ `กรีนคอส` (ยืนยันกับผู้ใช้อีกครั้ง); prompt สร้างจากแถว `crops` ไม่ฝังในโค้ด
- สถานีแปลงจาก `rfid_uid` ฝั่ง Server; UID ไม่รู้จัก → ไม่เดาสถานี
- จำนวนสถานี = แถว `active` ใน `stations` (เริ่ม 3 + 1 แถว `home`) ห้ามฝังเลข 3
- ภาพเสีย (เบลอ/มืด/สว่างเกิน) ต้องไม่ถูกส่งให้ Claude; ถ่ายซ้ำได้สูงสุด 2 ครั้งต่อสถานีต่อรอบ แล้วทำเครื่องหมาย `needs_review`
- ความมั่นใจต่ำ (< 0.6, ค่าเริ่มต้นปรับได้) → `needs_review`
- Claude เรียกผ่าน Anthropic SDK เท่านั้น; ชื่อรุ่นอ่านจาก env `NEXXA_VISION_MODEL`; ผู้ implement ต้องอ่าน skill `claude-api` เพื่อไวยากรณ์ที่ถูกต้อง (structured output, ตรวจ `stop_reason == "refusal"`, fallbacks)
- API key อ่านจาก env `ANTHROPIC_API_KEY` ห้ามเขียนลงไฟล์/ที่ commit
- ภาพที่โหลดจากเน็ตเป็นข้อมูลไม่น่าเชื่อถือ: เก็บในโฟลเดอร์ของตัวเอง, ตรวจว่าเป็นภาพจริงด้วย OpenCV ก่อนใช้, ไม่รันอะไรจากโฟลเดอร์นั้น

## Review Focus

1. ไฟล์ที่ไม่ใช่ภาพ/ภาพเสียหายถูกอัปโหลด → 422 ไม่ crash (Task 2, 4)
2. ภาพดำสนิท/ขาวล้วน → ถูกปฏิเสธเป็นภาพเสีย (Task 2)
3. Claude ตอบ label นอกรายการ หรือถูกปฏิเสธ (`refusal`) หรือ API ล่ม → บันทึก `needs_review` ไม่ crash ไม่เดา (Task 3, 4)
4. อัปโหลดซ้ำเกินจำนวนถ่ายซ้ำ → ไม่วนไม่สิ้นสุด (Task 4)
5. RFID ที่ไม่รู้จัก / RFID ของ Home ส่งมาเป็นสถานีตรวจ → ปฏิเสธชัดเจน (Task 4)

---

### Task 1: โครงโปรเจกต์ + ฐานข้อมูล + ข้อมูลตั้งต้น

**Files:**
- Create: `server/pyproject.toml` (หรือ `requirements.txt`), `server/app/__init__.py`, `server/app/config.py`, `server/app/db.py`, `server/app/models.py`, `server/app/seed.py`
- Test: `server/tests/test_models.py`

**Interfaces:**
- Produces: `config.settings` (`database_url`, `image_dir`, `vision_model`, `max_retakes=2`, `min_confidence=0.6`); `db.get_session() -> Iterator[Session]`; `db.init_db(engine) -> None`; ORM `Crop(id, name, labels: list[str], prompt, notes)`, `Station(id, rfid_uid unique, name, description, kind: "station"|"home", map_x, map_y, route_order, active, crop_id)`, `Round(id, started_at, status)`, `Capture(id, round_id, station_id, camera_angle, attempt, image_path, taken_at, quality_ok, quality_reason, crop_id)`, `Analysis(id, capture_id, label, confidence, note, model, needs_review)`; `seed.seed(session) -> None` (idempotent)

- [ ] **Step 1: เขียนเทสต์ที่ล้มเหลว** (`test_models.py`): หลัง `init_db` + `seed` บน SQLite in-memory → มี crop `กรีนคอส` ที่ `labels == ["ปกติ","ใบเหลือง","จุดน้ำตาล"]`, มี station `kind="station"` และ `active` 3 แถว, มี `kind="home"` 1 แถว, เรียก `seed` ซ้ำแล้วจำนวนแถวเท่าเดิม, และใส่ `rfid_uid` ซ้ำแล้วเกิด `IntegrityError`
- [ ] **Step 2: รันให้ล้ม** — `cd server && pytest tests/test_models.py -v` → FAIL (import error)
- [ ] **Step 3: สร้างโปรเจกต์ใน `server/` (git repo อยู่ที่ root `D:\Work\Nexxa` แล้ว ห้าม `git init` ซ้ำ), ติดตั้ง dependencies, เขียน config/db/models/seed ตาม Interfaces** — UID ใน seed เป็นค่าตัวอย่างชัดเจน (`TODO-REPLACE-1`…) เพื่อแทนด้วยค่าจริงตอนติดตั้งแท็ก
- [ ] **Step 4: รันให้ผ่าน** — `pytest tests/test_models.py -v` → PASS
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: schema and seed for crops and stations"`

### Task 2: ด่านกรองภาพ OpenCV

**Files:**
- Create: `server/app/quality.py`
- Test: `server/tests/test_quality.py`

**Interfaces:**
- Produces: `QualityResult(ok: bool, reason: str | None, blur_score: float, brightness: float)` (dataclass); `check_quality(image_bytes: bytes) -> QualityResult`; `ImageDecodeError(Exception)` เมื่อ bytes ไม่ใช่ภาพ

- [ ] **Step 1: เขียนเทสต์ที่ล้มเหลว** — สร้างภาพสังเคราะห์ด้วย numpy/cv2 ในเทสต์: (a) ภาพมีลวดลายคม → `ok=True`; (b) ภาพเดียวกันผ่าน `cv2.GaussianBlur` แรง → `ok=False, reason="blurry"`; (c) ภาพดำล้วน → `reason="too_dark"`; (d) ภาพขาวล้วน → `reason="too_bright"`; (e) `b"not an image"` → raise `ImageDecodeError`
- [ ] **Step 2: รันให้ล้ม** — `pytest tests/test_quality.py -v` → FAIL
- [ ] **Step 3: implement `check_quality`** — `cv2.imdecode`; เบลอ = variance ของ Laplacian บนภาพเทา (เกณฑ์เริ่มต้น `< 60`); แสง = ค่าเฉลี่ยภาพเทา (`< 40` มืด, `> 220` สว่าง) เก็บเกณฑ์เป็นค่าคงที่ที่หัวไฟล์พร้อมคอมเมนต์ `shortcut: เกณฑ์เริ่มต้น ปรับด้วยภาพจริงจาก ESP32-CAM` (ภาพดำ/ขาวล้วนต้องไม่ถูกจัดเป็น "เบลอ" ก่อนเช็กแสง — เช็กแสงก่อน)
- [ ] **Step 4: รันให้ผ่าน** → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: opencv quality gate"`

### Task 3: ตัววิเคราะห์ภาพด้วย Claude

**Files:**
- Create: `server/app/analyzer.py`
- Test: `server/tests/test_analyzer.py` (ใช้ client ปลอม ไม่เรียก API จริง)

**Interfaces:**
- Consumes: `models.Crop`; `config.settings.vision_model`
- Produces: `AnalysisResult(label: str | None, confidence: float, note: str, needs_review: bool, model: str)`; `build_prompt(crop: Crop) -> str`; `analyze_leaf(image_bytes: bytes, crop: Crop, client=None) -> AnalysisResult` (`client` ฉีดได้เพื่อเทสต์; ไม่ส่งก็สร้าง `anthropic.Anthropic()`)

- [ ] **Step 1: เขียนเทสต์ที่ล้มเหลว** — (a) `build_prompt` มีชื่อผัก, ทุก label ใน `crop.labels`, และ `crop.notes` (เรื่องใบแก่เหลือง/tipburn); (b) client ปลอมตอบ `{"label":"ใบเหลือง","confidence":0.9,"note":"x"}` → `needs_review=False`; (c) confidence 0.4 → `needs_review=True`; (d) label `"โรคอื่น"` ที่ไม่อยู่ใน `crop.labels` → `label=None, needs_review=True`; (e) client ปลอมคืน `stop_reason="refusal"` → `needs_review=True, label=None`; (f) client ปลอม raise exception ของ SDK → `needs_review=True, label=None` (ไม่ raise ออกมา)
- [ ] **Step 2: รันให้ล้ม** — `pytest tests/test_analyzer.py -v` → FAIL
- [ ] **Step 3: implement ตามลายเซ็น** — ส่งภาพเป็น base64 image block + prompt; บังคับรูปแบบตอบด้วย structured output (`output_config.format` JSON schema ที่ `label` เป็น enum จาก `crop.labels`) ตามที่ skill `claude-api` ระบุ; ตรวจ `stop_reason` ก่อนอ่าน content; ใส่ fallbacks ตาม skill; ความมั่นใจต่ำกว่า `settings.min_confidence` → `needs_review`
- [ ] **Step 4: รันให้ผ่าน** → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: claude leaf analyzer"`

### Task 4: Endpoint รับภาพ

**Files:**
- Create: `server/app/main.py`, `server/app/storage.py`
- Test: `server/tests/test_api.py`

**Interfaces:**
- Consumes: `check_quality`, `ImageDecodeError`, `analyze_leaf`, ORM ทั้งหมดจาก Task 1
- Produces: `storage.save_image(image_bytes: bytes, round_id: int, station_id: int, attempt: int) -> str` (คืน path); `POST /captures` (multipart: `image`, `rfid_uid`, `round_id`, `camera_angle`) → JSON `{capture_id, status: "analyzed"|"retake"|"needs_review", label, confidence, attempt}`; `POST /rounds` → `{round_id}`

- [ ] **Step 1: เขียนเทสต์ที่ล้มเหลว** (TestClient, `analyze_leaf` ถูก monkeypatch): (a) ภาพดี + RFID สถานี 1 → 200, `status="analyzed"`, มีแถว `Capture` + `Analysis` ผูก `station_id`/`round_id`; (b) ภาพเบลอ → `status="retake"`, ไม่มีแถว `Analysis`, `analyze_leaf` ไม่ถูกเรียก; (c) เบลอ 3 ครั้งติด (ถ่ายซ้ำเกิน `max_retakes`) → ครั้งที่ 3 `status="needs_review"`; (d) RFID ไม่รู้จัก → 404; (e) RFID ของ `home` → 422; (f) ไฟล์ไม่ใช่ภาพ → 422; (g) `analyze_leaf` คืน `needs_review=True` → `status="needs_review"` และบันทึกผลไว้
- [ ] **Step 2: รันให้ล้ม** — `pytest tests/test_api.py -v` → FAIL
- [ ] **Step 3: implement routes** — ลำดับ: หา station จาก `rfid_uid` → decode/quality → ถ้าเสียให้บันทึก `Capture(quality_ok=False)` และคำนวณ `attempt` จากจำนวน capture เดิมของ (round, station) → ถ้าดี เรียก `analyze_leaf` ด้วย `station.crop` → บันทึก `Analysis`; จำกัดขนาดไฟล์อัปโหลด (เช่น 5 MB) คืน 413 เกินนั้น
- [ ] **Step 4: รันให้ผ่าน** → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: capture upload endpoint"`

### Task 5: ชุดภาพทดสอบ ~30 ภาพจากเน็ต + สคริปต์วัดผล

**Files:**
- Create: `server/eval/manifest.csv` (`filename,label,source_url,license,note`), `server/eval/images/` (ไม่ commit ภาพ), `server/eval/run_eval.py`
- Test: `server/tests/test_eval_metrics.py`

**Interfaces:**
- Consumes: `check_quality`, `analyze_leaf`, `Crop` (seed)
- Produces: `compute_metrics(rows: list[tuple[str, str | None]]) -> dict` (`accuracy`, `confusion[true][pred]`, `n`; pred `None` นับเป็นผิด); `python -m eval.run_eval --manifest eval/manifest.csv` พิมพ์ accuracy, confusion matrix, จำนวนภาพที่ถูก quality gate ปฏิเสธ, เวลา/ภาพ และจำนวน token (จาก `usage`) — **รันจริงต้องได้รับ "ใช่" จากผู้ใช้ก่อนเพราะเสียเงิน**

- [ ] **Step 1: เขียนเทสต์ที่ล้มเหลว** สำหรับ `compute_metrics` — 4 แถว (3 ถูก, 1 pred=`None`) → `accuracy == 0.75` และ `confusion` ถูกต้อง
- [ ] **Step 2: รันให้ล้ม** → FAIL
- [ ] **Step 3: implement `compute_metrics` + `run_eval.py`** (อ่าน manifest, ผ่าน `check_quality` ก่อน, เรียก `analyze_leaf`, สรุปผล)
- [ ] **Step 4: รันให้ผ่าน** → PASS
- [ ] **Step 5: รวบรวมภาพ ~30 ภาพ** ที่ `eval/images/` — กรีนคอส/ผักสลัดใบปกติ ~10, ใบเหลือง ~10, จุดน้ำตาล ~8, ขอบใบไหม้ tipburn ~2–4 (label = `จุดน้ำตาล`, `note` ระบุ tipburn — ผู้ใช้ยังไม่ยืนยัน จึงถือเป็นค่าเริ่มต้นที่ปรับได้ใน `crops.notes`/prompt) และใบแก่รอบนอกเหลืองตามธรรมชาติ (label = `ปกติ`); ดาวน์โหลดจากแหล่งที่ใบอนุญาตระบุชัด (เช่น Wikimedia Commons CC) บันทึก `source_url`+`license` ทุกภาพ; ตรวจแต่ละไฟล์เปิดเป็นภาพได้ด้วย OpenCV; **ผู้ใช้/คนปลูกต้องตรวจป้าย label** เพราะป้ายจากคำบรรยายหน้าเว็บไม่น่าเชื่อถือ
- [ ] **Step 6: ขออนุมัติค่าใช้จ่ายแล้วรัน eval** — `python -m eval.run_eval --manifest eval/manifest.csv` ด้วย `NEXXA_VISION_MODEL` อย่างน้อย 1 รุ่น (เทียบ 2 รุ่นถ้าผู้ใช้ต้องการ) รายงานผลตามจริง รวมภาพที่ผิด
- [ ] **Step 7: Commit** — `git add eval/manifest.csv eval/run_eval.py tests && git commit -m "feat: eval harness and manifest"`

---

## Self-Review

- **Spec coverage:** crops/stations/rounds/captures/analyses (T1) · OpenCV กรอง (T2) · Claude label คงที่ + needs_review (T3) · RFID→สถานี, ถ่ายซ้ำ, ผูก station+round (T4) · ชุดทดสอบ 20–30 ภาพ + วัด % (T5) **ยังไม่อยู่ในแผนนี้:** Dashboard, Telegram, รายงานสรุป, ถามตอบ, เฟิร์มแวร์ ESP32 (ขั้น 3–6), การเก็บภาพเมื่อ Wi-Fi หลุด, ย้ายภาพไป object storage, deploy ขึ้น hosting ฟรี
- **ข้อจำกัดที่ต้องบอก:** ภาพจากเน็ตไม่ใช่ภาพ ESP32-CAM ในโรงเรือนกางมุ้ง ความแม่นยำที่วัดได้จะ**ดีเกินจริง**; ใช้ได้เปรียบเทียบรุ่น/ prompt เท่านั้น ต้องวัดซ้ำด้วยภาพจริงจากกล้อง
