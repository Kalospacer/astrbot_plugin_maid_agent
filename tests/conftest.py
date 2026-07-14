from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault(
    "ASTRBOT_ROOT",
    str(Path(tempfile.gettempdir()) / "astrbot-maid-agent-tests"),
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
