"""Per-room message catalogs; no message text is translated at import time."""
import json
from functools import lru_cache
from pathlib import Path

_CATALOG_DIR = Path(__file__).resolve().parent / "locales"

@lru_cache(maxsize=2)
def load_room_messages(language: str) -> dict[str, str]:
    language = "en" if str(language or "").lower().startswith("en") else "ar"
    path = _CATALOG_DIR / f"room_messages_{language}.json"
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError(f"Invalid room message catalog: {path.name}")
    return data

def load_english_room_messages() -> dict[str, str]:
    return load_room_messages("en")
