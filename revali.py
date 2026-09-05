#!/usr/bin/env python3
"""Entry point: python revali.py <subcommand> ...  (see revali/cli.py)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from revali.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
