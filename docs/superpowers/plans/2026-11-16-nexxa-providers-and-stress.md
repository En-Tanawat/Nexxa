# NEXXA Multi-Provider Vision + Offline Stress Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) ให้ analyzer เลือกผู้ให้บริการ AI ได้: Claude (ค่าเริ่มต้น), GPT, Gemini, Qwen, DeepSeek; (2) ชุดทดสอบจำลองแบบไม่ใช้ API key รันได้ 100,000 รอบ เพื่อยืนยันว่าระบบไม่พังในสภาพภาพต่าง ๆ

**Architecture:** Claude ใช้ Anthropic SDK เดิม ส่วนอีก 4 เจ้าใช้อะแดปเตอร์เดียวแบบ OpenAI-compatible (`openai` SDK + `base_url` ต่างกัน) ใช้ตัวตรวจคำตอบ (parse/validate/needs_review) ร่วมกัน ชุดทดสอบจำลองสร้างภาพสังเคราะห์หลายสภาพแล้วยิงผ่าน `check_quality` และผ่าน endpoint จริงด้วย analyzer ปลอม

**Tech Stack:** Python 3.11, anthropic, openai (ใหม่), OpenCV, numpy, FastAPI TestClient, pytest

**Spec:** `docs/2026-11-15-nexxa-design.md`; ต่อยอดจาก `docs/superpowers/plans/2026-11-15-nexxa-server-pipeline.md` (เสร็จแล้วบนสาขา `feat/server-pipeline`)

## Global Constraints

- ห้ามมี API key ในโค้ด/เทสต์/manifest; key อ่านจาก env เท่านั้น: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`
- ทุกเทสต์และชุดทดสอบจำลอง **ห้ามเรียกเครือข่ายจริง** (ฉีด client ปลอม)
- เลือกผู้ให้บริการด้วย env `NEXXA_VISION_PROVIDER` ∈ {`anthropic`(ค่าเริ่มต้น), `openai`, `gemini`, `qwen`, `deepseek`}; ชื่อรุ่นจาก `NEXXA_VISION_MODEL` — **ผู้ให้บริการที่ไม่ใช่ anthropic ไม่มีรุ่นเริ่มต้น** (ไม่เดาชื่อรุ่น) ถ้าไม่ตั้งค่า → ผลเป็น `needs_review` พร้อม note บอกชัดเจน
- Base URL: gemini `https://generativelanguage.googleapis.com/v1beta/openai/`; deepseek `https://api.deepseek.com`; openai ใช้ค่าเริ่มต้นของ SDK; qwen `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` (ค่านี้มาจากเอกสารบุคคลที่สาม และ Alibaba ใช้ URL เฉพาะ workspace/ภูมิภาค ตามเอกสารปัจจุบัน — ผู้ใช้ต้องตั้ง `NEXXA_VISION_BASE_URL` ทับได้); `NEXXA_VISION_BASE_URL` ทับของทุกเจ้า
- ผลจากทุกเจ้าผ่านตัวตรวจเดียวกัน: label ต้องอยู่ใน `crop.labels`, confidence 0..1, ความมั่นใจ < `settings.min_confidence` → `needs_review`; ข้อผิดพลาดใด ๆ → label None, confidence 0.0, needs_review True, **ไม่ raise** และไม่ log/แสดง key
- ไม่แก้พฤติกรรมเดิมของเส้นทาง anthropic (เทสต์เดิม 56 ตัวต้องผ่านต่อ); ไม่เพิ่ม dependency นอกจาก `openai`
- ชุดทดสอบจำลองต้อง deterministic (รับ `--seed`) และรายงานชัดว่า **ไม่ได้วัดความแม่นยำการตรวจโรค**

## Review Focus

1. คำตอบจากเจ้าอื่นห่อด้วย ```json ... ``` หรือมีข้อความนำหน้า → ยัง parse ได้หรือ needs_review ไม่ crash (Task 6)
2. `finish_reason` เป็น `length` / `content_filter` / `choices` ว่าง / `content` เป็น None → needs_review ไม่ crash (Task 6)
3. เลือก provider ที่ไม่รู้จัก, ไม่มี key ใน env, ไม่ได้ตั้งรุ่น → ผลเป็น needs_review พร้อมข้อความที่ชี้สาเหตุ ไม่ใช่ "อ่านคำตอบไม่ได้" (Task 6)
4. ชุดจำลองเจอไฟล์ว่าง/ถูกตัดกลางคัน/ภาพ 1×1/ภาพใหญ่เกินเพดาน → ไม่มี exception หลุด (Task 8)
5. ภาพที่ quality gate ปฏิเสธ ไม่เคยถูกส่งต่อให้ analyzer แม้แต่ครั้งเดียวใน 10,000 คำขอ (Task 8)

---

### Task 6: ผู้ให้บริการหลายเจ้าใน analyzer

**Files:**
- Create: `server/app/providers.py`, `server/tests/test_providers.py`
- Modify: `server/app/analyzer.py`, `server/app/config.py`, `server/requirements.txt`, `server/tests/test_analyzer.py` (เฉพาะถ้าต้องปรับ import)

**Interfaces:**
- Consumes: `analyzer` ปัจจุบัน (`build_prompt`, `AnalysisResult`, `_fail`, ตัว parse/validate ของ Task 3) และ `settings`
- Produces:
  - `config.settings.vision_provider: str` (env `NEXXA_VISION_PROVIDER`, ค่าเริ่มต้น `"anthropic"`), `settings.vision_base_url: str | None` (env `NEXXA_VISION_BASE_URL`)
  - `providers.Provider` (dataclass: `name`, `kind: Literal["anthropic","openai_compat"]`, `key_env: str`, `base_url: str | None`)
  - `providers.PROVIDERS: dict[str, Provider]` ครบ 5 เจ้า; `providers.get_provider(name: str) -> Provider` raise `UnknownProviderError(ValueError)` เมื่อไม่รู้จัก
  - `analyzer.analyze_leaf(image_bytes: bytes, crop: Crop, client=None, provider: str | None = None) -> AnalysisResult` — `provider=None` ใช้ `settings.vision_provider`; `client` ฉีดได้ทั้งสองแบบ (anthropic-shaped หรือ openai-shaped ตาม provider)

- [ ] **Step 1: เขียนเทสต์ที่ล้มเหลว** (`test_providers.py`, client ปลอมรูปแบบ openai: `.chat.completions.create(**kw)` คืนวัตถุที่มี `.choices[0].message.content`, `.choices[0].finish_reason`, `.model`): (a) `get_provider("gemini").base_url` ลงท้าย `/v1beta/openai/`, `"deepseek"` ได้ `https://api.deepseek.com`, `get_provider("xyz")` raise `UnknownProviderError`, ทั้ง 5 ชื่อมี `key_env` ตาม Global Constraints; (b) provider=`openai` + model ตั้งไว้ + คำตอบ `{"label":"ใบเหลือง","confidence":0.9,"note":"x"}` → label/needs_review ถูกต้อง และ `kwargs["model"]` เท่ากับที่ตั้ง, ข้อความผู้ใช้มี content 2 ส่วน (`text`, `image_url` ที่ขึ้นต้น `data:image/jpeg;base64,`), **ไม่มี** `temperature`/`response_format` ในคำขอ; (c) คำตอบห่อ ```json fence และมีข้อความนำ → parse ได้; (d) `finish_reason="length"` → needs_review (note บอกว่าถูกตัด); `"content_filter"` → needs_review; `choices=[]` → needs_review; `content=None` → needs_review; (e) provider ที่ไม่ใช่ anthropic แต่ไม่ได้ตั้ง `NEXXA_VISION_MODEL` → needs_review และ note พูดถึง `NEXXA_VISION_MODEL`; (f) ไม่มี key ใน env (ไม่ฉีด client) → needs_review และ note ระบุชื่อ env var ที่ต้องตั้ง **โดยไม่มีค่า key**; (g) `provider="xyz"` → needs_review ไม่ raise; (h) client ปลอม raise `openai.APIError`/`TypeError` → needs_review และมี log record (`caplog`); (i) label นอก `crop.labels` และ confidence ต่ำ ทำงานเหมือนเส้นทาง anthropic (ใช้ตัวตรวจร่วม)
- [ ] **Step 2: รันให้ล้ม** — `cd server && .venv/Scripts/python -m pytest tests/test_providers.py -v` → FAIL
- [ ] **Step 3: implement** — แยกตัว parse/validate ออกเป็นฟังก์ชันร่วม (เช่น `_finalize(raw_text, crop, model) -> AnalysisResult`) ใช้ทั้งสองเส้นทางโดยไม่เปลี่ยนพฤติกรรม anthropic เดิม; เส้นทาง openai_compat: prompt = `build_prompt(crop)` + คำสั่งให้ตอบเป็น JSON ล้วนตามโครงสร้าง `{label, confidence, note}` (ไม่ใช้ `response_format`/tool เพราะแต่ละเจ้ารองรับไม่เท่ากัน), ภาพเป็น data URI, `max_tokens=8192`, `timeout=60`; ตัด code fence/ข้อความนำด้วยการดึง JSON object แรก; ติดตั้ง `openai` ใน `server/.venv` และเพิ่มใน `requirements.txt` (ปักเวอร์ชันที่ติดตั้ง)
- [ ] **Step 4: รันให้ผ่าน** — `pytest tests/test_providers.py tests/test_analyzer.py -v` → PASS และ `pytest -q` ทั้งชุดผ่าน (56 เดิม + ใหม่)
- [ ] **Step 5: Commit** — `git add server && git commit -m "feat: multi-provider vision (openai, gemini, qwen, deepseek)"`

### Task 7: `--provider` ใน eval harness

**Files:**
- Modify: `server/eval/run_eval.py`, `server/tests/test_eval_metrics.py`

**Interfaces:**
- Consumes: `providers.get_provider`, `providers.PROVIDERS`, `analyzer.analyze_leaf(..., provider=...)`
- Produces: `python -m eval.run_eval --provider {anthropic,openai,gemini,qwen,deepseek}` (ค่าเริ่มต้นจาก `settings.vision_provider`); ตัวล็อกค่าใช้จ่ายเดิมตรวจ `key_env` ของ provider ที่เลือก (ต้องมี key ของเจ้านั้น **และ** `--confirm-spend`)

- [ ] **Step 1: เขียนเทสต์ที่ล้มเหลว**: `main([...])` ด้วย `--provider gemini` โดยไม่มี `GEMINI_API_KEY` → คืนรหัส 2 และข้อความพูดถึง `GEMINI_API_KEY` (ไม่ใช่ `ANTHROPIC_API_KEY`); มี key แต่ไม่มี `--confirm-spend` → 2; `--provider xyz` → 2 ด้วยข้อความชัดเจน; `--dry-run` ทุก provider ไม่เรียก analyzer (ตรวจด้วย monkeypatch ที่ล้มถ้าถูกเรียก); เทสต์ guard เดิมของ anthropic ยังผ่าน
- [ ] **Step 2: รันให้ล้ม** → FAIL
- [ ] **Step 3: implement** ตามลายเซ็นข้างบน (ห้ามพิมพ์ key; พิมพ์ชื่อ provider และรุ่นเท่านั้น)
- [ ] **Step 4: รันให้ผ่าน** — เทสต์ชุดนี้และชุดเต็มผ่าน
- [ ] **Step 5: Commit** — `git commit -m "feat: eval --provider with per-provider spend guard"`

### Task 8: ชุดทดสอบจำลองแบบไม่ใช้ API key

**Files:**
- Create: `server/eval/stress.py`, `server/tests/test_stress.py`

**Interfaces:**
- Consumes: `quality.check_quality`, `quality.ImageDecodeError`, `main.app` + dependency override/seed แบบเดียวกับ `tests/test_api.py`, ภาพจริงจาก `eval/images` ผ่าน `eval/manifest.csv` (ถ้ามี ใช้เป็นภาพตั้งต้นเพิ่ม; ไม่มีก็ใช้ภาพสังเคราะห์)
- Produces:
  - `CONDITIONS: tuple[str, ...]` อย่างน้อย: `sharp, blur_light, blur_heavy, dark, very_dark, bright, washed_out, noise, jpeg_low_quality, net_shadow, rotated, grayscale, png_alpha, tiny, oversize, truncated_jpeg, random_bytes, empty`
  - `make_condition_image(kind: str, rng: random.Random) -> bytes` (deterministic ตาม rng)
  - `run_quality_stress(n: int, seed: int) -> StressReport` — `StressReport` (dataclass): `n`, `per_condition: dict[str, dict[str,int]]` (นับ `ok`, `blurry`, `too_dark`, `too_bright`, `decode_error`), `unexpected_exceptions: list[str]`
  - `run_api_stress(n: int, seed: int) -> StressReport` — ยิง `POST /captures` ผ่าน TestClient (ฐานข้อมูลในหน่วยความจำ, header `X-Device-Key`, analyzer ปลอมที่นับครั้งที่ถูกเรียกและบันทึกว่าภาพที่ได้ผ่าน `check_quality` หรือไม่) พร้อมตรวจ invariants: รหัสสถานะอยู่ใน {200,404,409,413,422}, ไม่มี 500, `analyze_leaf` ไม่เคยได้รับภาพที่ quality gate ปฏิเสธ, ไม่มี Analysis ที่ผูกกับ capture ที่ `quality_ok=False`, จำนวน Capture เท่ากับจำนวนคำขอที่ถูกบันทึก, ไม่มีสถานะ `analyzed` ซ้ำสำหรับ (round, station, camera_angle) เดียวกัน
  - CLI: `python -m eval.stress --quality 100000 --api 10000 --seed 1` พิมพ์ตารางต่อสภาพ + รายการ invariant ที่ผ่าน/ไม่ผ่าน + คำเตือนบรรทัดเดียวว่า "นี่ไม่ได้วัดความแม่นยำของ AI ในการตรวจโรค"; รหัสออก 1 ถ้ามี invariant ล้ม

- [ ] **Step 1: เขียนเทสต์ที่ล้มเหลว** (จำนวนน้อยเพื่อให้ชุดเร็ว): (a) `make_condition_image` ทุกชนิดคืน bytes และ deterministic (seed เดิม → bytes เดิม); (b) `run_quality_stress(2000, seed=1)` → `unexpected_exceptions == []`, `empty`/`random_bytes`/`truncated_jpeg` ทั้งหมดเป็น `decode_error` หรือ (เฉพาะ truncated ที่ OpenCV ถอดได้บางส่วน) `ok`/เหตุผลที่ถูกต้อง, `very_dark` → ทั้งหมด `too_dark`, `washed_out` → ทั้งหมด `too_bright`, `blur_heavy` → `blurry` เกือบทั้งหมด (≥ 95%) และ `sharp` → `ok` เกือบทั้งหมด (≥ 95%), `oversize` → `decode_error`; (c) `run_api_stress(300, seed=1)` → ทุก invariant ผ่านและไม่มี 500; (d) รัน `main([...])` ของ CLI ด้วยจำนวนเล็ก (`--quality 200 --api 30`) คืนรหัส 0 และผลลัพธ์ที่พิมพ์มีคำเตือนเรื่องความแม่นยำ
- [ ] **Step 2: รันให้ล้ม** — `pytest tests/test_stress.py -v` → FAIL
- [ ] **Step 3: implement** — สร้างภาพฐานสังเคราะห์เล็ก (เช่น 320×240) ล่วงหน้าเป็น pool แล้วใช้เงื่อนไขทีละแบบ (blur ด้วย `cv2.GaussianBlur`, ลดแสงด้วยการคูณ, เติม Gaussian noise, บีบ JPEG คุณภาพต่ำ, วางกริดเงาตาข่ายมุ้งโปร่งแสง, หมุน, แปลงเทา, PNG มี alpha, ภาพ 8×8, ภาพ 5000×5000, ตัด bytes JPEG กลางคัน, สุ่ม bytes, bytes ว่าง) เพื่อให้ 100,000 รอบจบในเวลาสมเหตุผล; ไม่เรียกเครือข่าย
- [ ] **Step 4: รันให้ผ่าน** — เทสต์ชุดนี้ผ่านและ `pytest -q` ทั้งชุดผ่าน
- [ ] **Step 5: รันจริงเต็มขนาดครั้งเดียว** — `cd server && .venv/Scripts/python -m eval.stress --quality 100000 --api 10000 --seed 1` เก็บผลรวมและเวลา (ไม่ commit ผลลัพธ์); ถ้า invariant ล้ม ให้รายงานตามจริงพร้อมตัวอย่างอินพุตที่ทำให้ล้ม (seed + condition)
- [ ] **Step 6: Commit** — `git commit -m "feat: offline stress harness (no API key)"`

---

## Self-Review

- **Spec coverage:** รองรับ AI เจ้าอื่น (T6, T7) · ทดสอบ 10,000–100,000 รอบแบบไม่ใช้ key (T8) · ความแม่นยำการตรวจโรคด้วยโมเดลจริง **ไม่อยู่ในแผนนี้** (ต้องมี key ภาพจริงที่ผู้ปลูกยืนยัน และงบ — ใช้ `run_eval --provider ... --confirm-spend` ภายหลัง) · รายงานสรุป/ถามตอบ/Dashboard/Telegram **ยังไม่อยู่ในแผนนี้**
- **ข้อจำกัด:** ยังไม่ได้ยืนยันกับ API จริงของเจ้าอื่นว่ารับ `image_url` แบบ data URI ตามเอกสารที่อ่านจากการค้นเว็บ (เอกสาร DeepSeek/Qwen/Gemini ระบุว่ารองรับ, Gemini ยังไม่เห็นตัวอย่างภาพโดยตรง); ชื่อรุ่นที่ใช้ได้จริงขึ้นกับบัญชี/ภูมิภาคของผู้ใช้
