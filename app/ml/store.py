from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def load_meta(artifact_dir: Path) -> Optional[dict[str, Any]]:
    meta_path = artifact_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text())
    except Exception:
        return None
