from __future__ import annotations

import json

from .paths import get_toolkit_home


CHECKPOINT_PATH = get_toolkit_home() / "processes.json"


def _atomic_json_write(path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
