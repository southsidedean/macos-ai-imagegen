"""python -m mia: how the supervisor starts the worker, with the same Python it's running on."""

import sys

from mia.cli import main

sys.exit(main())
