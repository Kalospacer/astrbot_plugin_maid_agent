from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault(
    "ASTRBOT_ROOT",
    str(Path(tempfile.gettempdir()) / "astrbot-maid-agent-tests"),
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# AstrBot loads a plugin from a directory named after its metadata. The
# standalone checkout has a generic workspace name, so give tests the same
# package namespace without altering the production plugin layout.
PACKAGE_NAME = "astrbot_plugin_maid_agent"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(WORKSPACE_ROOT)]
sys.modules.setdefault(PACKAGE_NAME, package)
