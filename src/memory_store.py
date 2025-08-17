# memory_store.py (new file)
import json
from pathlib import Path
from typing import Dict, List, Tuple, Any

from load_config import MEMORY_STORE, MEMORY_MAX_PER_USER, MEMORY_MAX_CONTEXT_CHARS

# Type alias
Msg = Dict[str, str]  # {"role": "...", "content": "..."}

class MemoryStore:
    def __init__(self, path: Path = MEMORY_STORE):
        self.path = path
        self._cache: Dict[int, List[Msg]] = {}  # in‑memory copy
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf‑8"))
                # dict[str, list[dict]] -> dict[int, list]
                self._cache = {int(k): v for k, v in data.items()}
            except Exception:
                # keep silent – the bot should still work
                pass

    def _save(self) -> None:
        try:
            # convert int keys back to str for JSON
            json_data = {str(k): v for k, v in self._cache.items()}
            self.path.write_text(json.dumps(json_data, indent=2), encoding="utf‑8")
        except Exception:
            # ignore file‑write failures; what matters is runtime behaviour
            pass

    # ------------------------------------------------------------------ #
    # Public helpers
    # ------------------------------------------------------------------ #
    def get_user_messages(self, user_id: int) -> List[Msg]:
        """Return the current list of messages for *user_id*."""
        return list(self._cache.get(user_id, []))  # defensive copy

    def add_message(self, user_id: int, msg: Msg) -> None:
        mem = self._cache.setdefault(user_id, [])
        mem.append(msg)
        self._prune(user_id)
        self._save()

    def clear_user(self, user_id: int) -> None:
        if user_id in self._cache:
            del self._cache[user_id]
            self._save()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _prune(self, user_id: int) -> None:
        """Ensure storage limits for a single user."""
        mem = self._cache[user_id]

        # 1. Max number of messages
        while len(mem) > MEMORY_MAX_PER_USER:
            mem.pop(0)          # drop the oldest

        # 2. Max total characters (roughly)
        total_chars = sum(len(m["content"]) for m in mem)
        if total_chars > MEMORY_MAX_CONTEXT_CHARS:
            # Drop oldest until satisfy
            while mem and total_chars > MEMORY_MAX_CONTEXT_CHARS:
                removed = mem.pop(0)
                total_chars -= len(removed["content"])