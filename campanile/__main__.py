"""``python -m campanile`` -- the same entry point as the ``campanile`` script."""

from __future__ import annotations

import sys

from .cli.main import main

if __name__ == "__main__":
    sys.exit(main())
