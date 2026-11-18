"""``python -B -m dev_supervisor`` entry point (T-DEV-008). See ``cli.py`` for the commands."""
import sys

from .cli import main

sys.exit(main())
