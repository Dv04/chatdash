"""Forwards the old `python -m chatdash.statusline` status line command to dhi_orbit.statusline."""
import sys

from dhi_orbit.statusline import main

if __name__ == "__main__":
    sys.exit(main())
