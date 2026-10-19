"""`python -m pm.calibration`：与 `run.py calibrate` 同一个 main。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
