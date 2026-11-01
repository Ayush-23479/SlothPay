from __future__ import annotations

import uuid
from datetime import UTC, datetime

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MAX_CURSOR_LENGTH = 80


def format_cursor(created_at: datetime, row_id: uuid.UUID | str) -> str:
    """The cursor string for one event: <microseconds since epoch>.<uuid hex>."""
    stamp = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    delta = stamp - _EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return f"{micros}.{uuid.UUID(str(row_id)).hex}"


def parse_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    """Inverse of format_cursor. Raises ValueError for anything that is not a cursor."""
    if not isinstance(value, str) or not value or len(value) > MAX_CURSOR_LENGTH:
        raise ValueError("Invalid cursor")
    micros_text, dot, id_text = value.partition(".")
    if not dot or not micros_text.isascii() or not micros_text.isdigit() or len(id_text) != 32:
        raise ValueError("Invalid cursor")
    try:
        row_id = uuid.UUID(hex=id_text)
        micros = int(micros_text)
        created_at = datetime.fromtimestamp(micros // 1_000_000, tz=UTC).replace(
            microsecond=micros % 1_000_000
        )
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("Invalid cursor") from exc
    return created_at, row_id