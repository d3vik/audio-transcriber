#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Source-tree entry point; run with pipenv run python transcribe.py."""

import sys

if __name__ == '__main__':
    if sys.version_info < (3, 11):
        raise SystemExit('Python 3.11 or newer is required.')
    from src.cli import main

    raise SystemExit(main())
