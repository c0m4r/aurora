#!/usr/bin/env python3
"""Quick-start: run the Aurora server."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from aurora.api.app import run

if __name__ == "__main__":
    run()
