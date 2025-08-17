#!/usr/bin/env python3
# coding: utf-8

"""
mikaz.py – launcher for the Discord bot

The project layout is

    project/
        .gitignore
        requirements.txt
        config/
            authorized.json
            config.json
            memory.json
            sys_prompt.json
        src/
            load_config.py
            call_api.py
            functions.py
            main.py            ← contains the bot definition
        mikaz.py              ← this file
        

`mikaz.py` simply makes *src* discoverable, imports the
`main` module that builds the bot, and starts the bot.

It keeps the same logging configuration already defined in
`src/main.py`; therefore the one‑liner below is all that is needed
to start the bot from the project root.
"""


import sys
import os

# ---------------------------------------------------------------------
# Make the src package importable (project root is the parent directory)
# ---------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))   # <project>
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# ---------------------------------------------------------------------
# Import the module that contains the bot and its configuration
# ---------------------------------------------------------------------
from src import main

# ---------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------
if __name__ == "__main__":
    try:
        # The token is loaded by `load_config` inside `main.py`,
        # so we can use it straight away.
        main.bot.run(main.load_config.DISCORD_TOKEN)
    except Exception:  # pragma: no cover
        import traceback
        traceback.print_exc()