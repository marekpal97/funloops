"""``python -m devloop`` — the same entry point as the ``devloop`` console script."""

import sys

from devloop.cli import main

if __name__ == "__main__":
    sys.exit(main())
