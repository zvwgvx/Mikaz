# --------------------------------------------------
# config.py  
# --------------------------------------------------
# Configuration module that loads settings exclusively from JSON files.
# No support for .env or .txt files – everything is JSON only.

import json
import logging
from pathlib import Path

# --------------------------------------------------------------------
# Logger
# --------------------------------------------------------------------
logger = logging.getLogger("discord-openai-proxy.config")

# --------------------------------------------------------------------
# Directories and file paths
# --------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # root/
CONF_DIR = BASE_DIR / "config"     
# --------------------------------------------------------------------
# Load env.json
# --------------------------------------------------------------------
ENV_FILE = CONF_DIR / "config.json"
SYS_PROMPT_FILE = CONF_DIR / "sys_prompt.json"
AUTHORIZED_STORE = CONF_DIR / "authorized.json"
MEMORY_STORE     = CONF_DIR / "memory.json"

try:
    env_text = ENV_FILE.read_text(encoding="utf-8")
    env_data: dict = json.loads(env_text)
    if not isinstance(env_data, dict):
        raise ValueError("env.json must be a JSON object.")
except FileNotFoundError:
    logger.error(f"{ENV_FILE} not found.")
    env_data = {}
except json.JSONDecodeError as exc:
    logger.error(f"JSON error in {ENV_FILE}: {exc}")
    env_data = {}
except Exception as exc:
    logger.exception(f"Unhandled error reading {ENV_FILE}: {exc}")
    env_data = {}

# --------------------------------------------------------------------
# Environment variables extracted from env.json
# --------------------------------------------------------------------
DISCORD_TOKEN            = env_data.get("DISCORD_TOKEN")
OPENAI_API_KEY           = env_data.get("OPENAI_API_KEY")
OPENAI_API_BASE          = env_data.get("OPENAI_API_BASE")
OPENAI_MODEL             = env_data.get("OPENAI_MODEL")

REQUEST_TIMEOUT          = int(env_data.get("REQUEST_TIMEOUT"))
MAX_MSG                  = int(env_data.get("MAX_MSG"))
MEMORY_MAX_PER_USER      = int(env_data.get("MEMORY_MAX_PER_USER"))
MEMORY_MAX_CONTEXT_CHARS = int(env_data.get("MEMORY_MAX_CONTEXT_CHARS"))

# --------------------------------------------------------------------
# Mandatory checks
# --------------------------------------------------------------------
if DISCORD_TOKEN is None or OPENAI_API_KEY is None:
    raise RuntimeError(
        "DISCORD_TOKEN and OPENAI_API_KEY must be defined in env.json "
        "(or set as regular environment variables)."
    )

# --------------------------------------------------------------------
# Other configuration constants
# --------------------------------------------------------------------

# --------------------------------------------------------------------
# System prompt loader
# --------------------------------------------------------------------
def load_system_prompt() -> dict:
    """
    Return a dict of the form {'role':'system', 'content': ...}.
    Reads sys_prompt.json; if the file is missing or empty, a default
    Vietnamese prompt is returned.
    """
    default = (
        "I am a cp-er who focuses primarily on C++ with a strong emphasis "
        "on algorithms and reasoning. All interactions in Vietnamese, "
        "and you should refer to yourself as 'miss' and to the user as 'anh'."
    )

    if SYS_PROMPT_FILE.exists():
        try:
            raw = SYS_PROMPT_FILE.read_text(encoding="utf-8").strip()
            if raw:
                # Accept either plain string or { "content": "..." }
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and "content" in obj:
                        raw = obj["content"].strip()
                except json.JSONDecodeError:
                    # raw remains as the original string
                    pass
                return {"role": "system", "content": raw}
        except Exception as exc:
            logger.exception(f"Failed to read {SYS_PROMPT_FILE}: {exc}")

    # Fallback to a generic prompt if file missing/empty
    return {"role": "system", "content": default}