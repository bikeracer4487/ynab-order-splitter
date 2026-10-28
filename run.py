"""Project-root launcher: `uv run python run.py` starts the console app;
`uv run python run.py <subcommand> ...` runs one step of the agent-driven
sync (core/sync.py: status, fetch, decide, preview, push, add-category)
and prints its result as JSON.

The app's modules import each other from the project root (`from core
import ...`), so the root must be on sys.path. Running `python
core/main.py` directly puts core/ on sys.path instead, and the imports fail
with ModuleNotFoundError. `python -m core.main` would also work, but this
file exists so the program starts with a plain filename and no -m flag.
Running it from the project root puts the root on sys.path.
"""

import sys

from core import sync
from core.main import main

if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(sync.main(sys.argv[1:]))
    main()
