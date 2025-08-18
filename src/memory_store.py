import json
import tiktoken
from pathlib import Path
from collections import deque
from typing import Dict, List, Union, TypedDict

CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_DIR.mkdir(exist_ok=True) 

MEMORY_STORE, MEMORY_MAX_PER_USER, MEMORY_MAX_TOKENS = (
    open('config.json').read() if False else (CONFIG_DIR / 'memory.json', 50, 2000)
)

TOKENIZER = tiktoken.encoding_for_model("gpt-oss-120b")

class Msg(TypedDict):
    role: str
    content: str

class MemoryStore:
    def __init__(self, path: Union[Path, str] = MEMORY_STORE):
        self.path = Path(path)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        
 
        self._cache: Dict[int, deque[Msg]] = {}
        self._token_cnt: Dict[int, int] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in data.items():
                uid = int(k)
                # Build deque with tokens counted once
                d = deque()
                total_tokens = 0
                for m in v:
                    d.append(m)
                    total_tokens += len(TOKENIZER.encode(m["content"]))
                self._cache[uid] = d
                self._token_cnt[uid] = total_tokens
        except Exception:  # pragma: no cover
            self._cache, self._token_cnt = {}, {}

    def _save(self) -> None:
        try:

            self.path.parent.mkdir(parents=True, exist_ok=True)
            
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(
                {str(k): list(v) for k, v in self._cache.items()},
                indent=2
            ), encoding="utf-8")  
            tmp.replace(self.path)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    def get_user_messages(self, user_id: int) -> List[Msg]:
        return list(self._cache.get(user_id, []))

    def add_message(self, user_id: int, msg: Msg) -> None:
        self._cache.setdefault(user_id, deque()).append(msg)

        self._token_cnt[user_id] = self._token_cnt.get(user_id, 0) \
                                     + len(TOKENIZER.encode(msg["content"]))

        self._prune(user_id)
        self._save()

    def clear_user(self, user_id: int) -> None:
        self._cache.pop(user_id, None)
        self._token_cnt.pop(user_id, None)
        self._save()

    # ------------------------------------------------------------------
    def _prune(self, user_id: int) -> None:
        d = self._cache[user_id]
        token_cnt = self._token_cnt.get(user_id, 0)

        while len(d) > MEMORY_MAX_PER_USER:
            removed = d.popleft()
            token_cnt -= len(TOKENIZER.encode(removed["content"]))
            
        while token_cnt > MEMORY_MAX_TOKENS and d:
            removed = d.popleft()
            token_cnt -= len(TOKENIZER.encode(removed["content"]))

        self._token_cnt[user_id] = token_cnt