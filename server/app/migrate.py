"""Tiny schema sync: create_all never ALTERs, so add columns that models gained later."""
import logging
import math

from sqlalchemy import Engine, MetaData, inspect

from app.models import Base

log = logging.getLogger(__name__)


def default_literal(value, dialect) -> str | None:
    """SQL literal for a simple scalar default, else None (never emit anything unsafe)."""
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE") if dialect.name == "postgresql" else ("1" if value else "0")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else None
    if isinstance(value, str) and "\x00" not in value:
        return "'" + value.replace("'", "''") + "'"
    return None


def ensure_columns(engine: Engine, metadata: MetaData = Base.metadata) -> list[str]:
    added: list[str] = []
    insp = inspect(engine)
    prep = engine.dialect.identifier_preparer
    for table in metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            name = f"{table.name}.{col.name}"
            try:
                ddl = f"ALTER TABLE {prep.format_table(table)} ADD COLUMN {prep.quote(col.name)} {col.type.compile(dialect=engine.dialect)}"
                dflt = default_literal(col.default.arg, engine.dialect) if col.default is not None and col.default.is_scalar else None
                if dflt is not None:
                    ddl += f" DEFAULT {dflt}"
                with engine.begin() as conn:
                    conn.exec_driver_sql(ddl)  # no bind parsing: literals may contain ':' or '%'
            except Exception as e:  # one failure must not stop the rest
                log.warning("could not add column %s (%s)", name, type(e).__name__)
                continue
            added.append(name)
            log.info("added column %s", name)
    return added
