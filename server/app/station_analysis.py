"""One vision call per station visit: all of the station's usable photos go to the AI together and it counts plants."""
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import analyzer, storage
from app.models import Capture, Crop, Station, StationAnalysis

log = logging.getLogger(__name__)
# The robot waits for the summary with a 60 s HTTP timeout; the optional AI wording needs up to 25 s after this.
ANALYSIS_BUDGET_S = 30


def usable_captures(session: Session, round_id: int, station_id: int) -> list[Capture]:
    """The newest quality-passed capture of each camera angle (a retake replaces the failed one), oldest first."""
    caps = session.scalars(select(Capture).where(
        Capture.round_id == round_id, Capture.station_id == station_id, Capture.quality_ok.is_not(False)).order_by(Capture.id)).all()
    return sorted({(c.camera_angle or f"#{c.id}"): c for c in caps}.values(), key=lambda c: c.id)


def _relative(image_path: str) -> str:
    """Path under the image directory (so the mapping survives moving that directory); unchanged when it is outside or in the DB."""
    if image_path.startswith(storage.DB_PREFIX):
        return image_path
    try:
        return Path(image_path).resolve().relative_to(storage.settings.image_dir.resolve()).as_posix()
    except ValueError:
        return image_path


def run(session: Session, round_id: int, station_id: int) -> StationAnalysis:
    """Analyse (or re-analyse) the station's photos and store the result, replacing any earlier row for this round and station."""
    caps = usable_captures(session, round_id, station_id)
    images, photos = [], []
    for c in caps:
        data = storage.load_image(session, c)
        if data is None:  # an old file-based photo that is gone (host restarted): analyse what is left
            continue
        images.append(data)
        photos.append({"capture_id": c.id, "angle": c.camera_angle, "path": _relative(c.image_path)})
    crop = session.get(Crop, session.get(Station, station_id).crop_id)
    session.commit()  # end the read transaction: the AI call below can take half a minute
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        r = pool.submit(analyzer.analyze_station, images, crop).result(timeout=ANALYSIS_BUDGET_S)
    except Exception as e:  # includes the budget TimeoutError; a straggler thread ends by its own client timeout
        log.warning("station analysis failed: %s", type(e).__name__)
        r = analyzer._fail("วิเคราะห์ไม่สำเร็จหรือเกินเวลา")
    finally:
        pool.shutdown(wait=False)
    session.execute(delete(StationAnalysis).where(StationAnalysis.round_id == round_id, StationAnalysis.station_id == station_id))
    row = StationAnalysis(
        round_id=round_id, station_id=station_id, n_photos=len(images), n_total=r.total, n_yellow=r.yellow, n_brown=r.brown,
        confidence=r.confidence, note=r.note, model=r.model, needs_review=r.needs_review or r.total is None, photos=photos)
    session.add(row)
    session.commit()
    return row
