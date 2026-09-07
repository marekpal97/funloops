"""Core: runs things.

More detail that must not reach the catalog.
"""

import json

from fx import helper

LIMIT = 5


def run(x: int) -> str:
    return helper.fmt(json.dumps(x))
