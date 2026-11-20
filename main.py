#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Entry point: ``python main.py`` runs the demo, ``python main.py --help``
lists the commands. Standard library only; nothing to install."""

import sys

if sys.version_info < (3, 9):
    sys.stderr.write("codemode-runtime needs Python 3.9 or newer; this is %s\n" % sys.version.split()[0])
    sys.exit(2)

from codemode_runtime.cli import main  # noqa: E402  (after the version check on purpose)

if __name__ == "__main__":
    sys.exit(main())
