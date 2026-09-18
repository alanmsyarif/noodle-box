"""Compatibility launcher. Install dist/noodle_box-1.0.0.zip for normal use.

Keep this file next to noodle_box/ to run the original headless commands or
load the development checkout in Blender's Text Editor.
"""
import sys
from pathlib import Path

# Only the development launcher adjusts sys.path; the installed extension
# uses normal relative imports and never modifies Blender's module paths.
root = str(Path(__file__).resolve().parent)
if root not in sys.path:
    sys.path.insert(0, root)

from noodle_box.solver import *  # noqa: F401,F403 - legacy script API


if __name__ == "__main__":
    try:
        main()
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
