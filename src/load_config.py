# --------------------------------------------------
# config.py  -  Improved version
# --------------------------------------------------
import json
import logging
from pathlib import Path
from typing import Any, Dict

# --------------------------------------------------------------------
# Logger
# --------------------------------------------------------------------
logger = logging.getLogger("discord-openai-proxy.config")
# Đặt level DEBUG để debug tin tức cấu hình
if not logger.handlers:  # tránh thêm nhiều console handler
    hdlr = logging.StreamHandler()
    fmt = "%(asctime)s %(name)s %(levelname)s: %(message)s"
    hdlr.setFormatter(logging.Formatter(fmt))
    logger.addHandler(hdlr)
    logger.setLevel(logging.INFO)

# --------------------------------------------------------------------
# Path constants
# --------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
CONF_DIR = BASE_DIR / "config"

ENV_FILE = CONF_DIR / "config.json"
SYS_PROMPT_FILE = CONF_DIR / "sys_prompt.json"
AUTHORIZED_STORE = CONF_DIR / "authorized.json"
MEMORY_STORE = CONF_DIR / "memory.json"

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def _load_json_file(path: Path) -> Dict[str, Any]:
    """Return an empty dict if file missing; raise warning if JSON bad."""
    if not path.exists():
        logger.warning(f"File không tồn tại: {path}")
        return {}
    try:
        content = path.read_text(encoding="utf-8")
        return json.loads(content) if content.strip() else {}
    except json.JSONDecodeError as exc:
        logger.error(f"format JSON sai {path}:\n{exc}")
        return {}
    except Exception as exc:
        logger.exception(f"Error đọc {path}: {exc}")
        return {}

def _int_or_default(val: Any, default: int, name: str) -> int:
    if val is None:  # key missing
        logger.warning(f"{name} không xác định trong config; dùng mặc định {default}")
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        logger.error(f"{name} phải là số nguyên; tạo {default}")
        return default

# --------------------------------------------------------------------
# Đọc env.json
# --------------------------------------------------------------------
env_data: Dict[str, Any] = _load_json_file(ENV_FILE)

# --------------------------------------------------------------------
# Environment variables
# --------------------------------------------------------------------
DISCORD_TOKEN = env_data.get("DISCORD_TOKEN")
OPENAI_API_KEY = env_data.get("OPENAI_API_KEY")
OPENAI_API_BASE = env_data.get("OPENAI_API_BASE")
OPENAI_MODEL = env_data.get("OPENAI_MODEL")

# Tham số toàn cục
REQUEST_TIMEOUT = _int_or_default(env_data.get("REQUEST_TIMEOUT"), 100, "REQUEST_TIMEOUT")
MAX_MSG = _int_or_default(env_data.get("MAX_MSG"), 1900, "MAX_MSG")
MEMORY_MAX_PER_USER = _int_or_default(env_data.get("MEMORY_MAX_PER_USER"), 10, "MEMORY_MAX_PER_USER")
MEMORY_MAX_TOKENS = _int_or_default(env_data.get("MEMORY_MAX_TOKENS"), 2500, "MEMORY_MAX_TOKENS")

# --------------------------------------------------------------------
# Mandatory checks
# --------------------------------------------------------------------
if DISCORD_TOKEN is None or OPENAI_API_KEY is None:
    raise RuntimeError(
        "DISCORD_TOKEN và OPENAI_API_KEY phải được khai báo trong config.json."
    )

# Nếu bạn muốn kiểm tra thêm
SUPPORTED_MODELS = {"gpt-oss-20b", "gpt-oss-120b", "gpt-5", "o3-mini"}
if OPENAI_MODEL and OPENAI_MODEL not in SUPPORTED_MODELS:
    logger.warning(f"MODEL {OPENAI_MODEL} không được liệt kê; nên giám sát sau này.")

# --------------------------------------------------------------------
# System prompt loader
# --------------------------------------------------------------------
DEFAULT_SYS_PROMPT = (
    "Bạn là một chuyên gia C++ tập trung vào thuật toán và tư duy. "
    "Tất cả tương tác diễn ra bằng tiếng Việt, bạn gọi người dùng là 'anh', "
    "bạn gọi mình là 'miss'."
)

def load_system_prompt() -> Dict[str, str]:
    """Trả về dict {'role':'system', 'content': ...}"""
    if SYS_PROMPT_FILE.exists():
        try:
            raw = SYS_PROMPT_FILE.read_text(encoding="utf-8").strip()
            if raw:
                # Số kiểu dict {"content": "..."} hay chuỗi thô
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and "content" in obj:
                        raw = obj["content"].strip()
                except json.JSONDecodeError:
                    pass
                logger.info(f"Dùng prompt từ {SYS_PROMPT_FILE}")
                return {"role": "system", "content": raw}
        except Exception as exc:
            logger.exception(f"Error đọc {SYS_PROMPT_FILE}: {exc}")

    logger.warning("Không tìm thấy sys_prompt.json; dùng mặc định.")
    return {"role": "system", "content": DEFAULT_SYS_PROMPT}