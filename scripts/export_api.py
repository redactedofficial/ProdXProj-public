"""Export the monitor's API discovery schema for other tools."""
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
from proxrunner.monitor import OPENAPI

destination = project_root / "docs" / "openapi.json"
destination.write_text(json.dumps(OPENAPI, indent=2) + "\n", encoding="utf-8")
print(destination)
