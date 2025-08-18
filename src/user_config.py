#!/usr/bin/env python3
# coding: utf-8
# user_config.py - Quản lý cấu hình riêng cho từng user

import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("discord-openai-proxy.user_config")

# Đường dẫn file cấu hình user
BASE_DIR = Path(__file__).resolve().parent.parent
CONF_DIR = BASE_DIR / "config"
USER_CONFIG_FILE = CONF_DIR / "user_config.json"

# Models được hỗ trợ
SUPPORTED_MODELS = {
    "o3-mini",
    "gpt-4.1"
    "gpt-5",
    "gpt-oss-20b",
    "gpt-oss-120b", 
}

# System prompt mặc định
DEFAULT_SYSTEM_PROMPT = (
    "Bạn là một chuyên gia C++ tập trung vào thuật toán và tư duy. "
    "Tất cả tương tác diễn ra bằng tiếng Việt, bạn gọi người dùng là 'anh', "
    "bạn gọi mình là 'miss'."
)

# Model mặc định
DEFAULT_MODEL = "gpt-5"

class UserConfigManager:
    def __init__(self):
        self.config_file = USER_CONFIG_FILE
        self._config_cache: Dict[str, Dict[str, Any]] = {}
        self._load_config()
    
    def _load_config(self) -> None:
        """Load cấu hình từ file JSON"""
        if not self.config_file.exists():
            self._config_cache = {}
            return
            
        try:
            content = self.config_file.read_text(encoding="utf-8")
            self._config_cache = json.loads(content) if content.strip() else {}
            logger.info(f"Loaded config for {len(self._config_cache)} users")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in user_config.json: {e}")
            self._config_cache = {}
        except Exception as e:
            logger.exception(f"Error loading user_config.json: {e}")
            self._config_cache = {}
    
    def _save_config(self) -> None:
        """Lưu cấu hình vào file JSON"""
        try:
            # Đảm bảo thư mục tồn tại
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Ghi vào file tạm trước
            tmp_file = self.config_file.with_suffix('.tmp')
            tmp_file.write_text(
                json.dumps(self._config_cache, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            
            # Sau đó move sang file chính
            tmp_file.replace(self.config_file)
            logger.debug(f"Saved config for {len(self._config_cache)} users")
            
        except Exception as e:
            logger.exception(f"Error saving user_config.json: {e}")
    
    def get_user_config(self, user_id: int) -> Dict[str, Any]:
        """Lấy cấu hình của user, tạo mặc định nếu chưa có"""
        user_key = str(user_id)
        
        if user_key not in self._config_cache:
            self._config_cache[user_key] = {
                "model": DEFAULT_MODEL,
                "system_prompt": DEFAULT_SYSTEM_PROMPT
            }
            self._save_config()
        
        return self._config_cache[user_key]
    
    def set_user_model(self, user_id: int, model: str) -> tuple[bool, str]:
        """
        Đặt model cho user
        Returns: (success: bool, message: str)
        """
        if model not in SUPPORTED_MODELS:
            supported_list = ", ".join(sorted(SUPPORTED_MODELS))
            return False, f"Model '{model}' không được hỗ trợ. Các model có sẵn: {supported_list}"
        
        user_config = self.get_user_config(user_id)
        user_config["model"] = model
        self._save_config()
        
        return True, f"Đã đặt model thành '{model}'"
    
    def set_user_system_prompt(self, user_id: int, prompt: str) -> tuple[bool, str]:
        """
        Đặt system prompt cho user
        Returns: (success: bool, message: str)
        """
        if not prompt.strip():
            return False, "System prompt không thể để trống"
        
        if len(prompt) > 10000:  # Giới hạn độ dài
            return False, "System prompt quá dài (tối đa 10,000 ký tự)"
        
        user_config = self.get_user_config(user_id)
        user_config["system_prompt"] = prompt.strip()
        self._save_config()
        
        return True, "Đã cập nhật system prompt"
    
    def get_user_model(self, user_id: int) -> str:
        """Lấy model hiện tại của user"""
        return self.get_user_config(user_id)["model"]
    
    def get_user_system_prompt(self, user_id: int) -> str:
        """Lấy system prompt hiện tại của user"""
        return self.get_user_config(user_id)["system_prompt"]
    
    def get_user_system_message(self, user_id: int) -> Dict[str, str]:
        """Lấy system message theo format OpenAI"""
        return {
            "role": "system",
            "content": self.get_user_system_prompt(user_id)
        }
    
    def reset_user_config(self, user_id: int) -> str:
        """Reset cấu hình user về mặc định"""
        user_key = str(user_id)
        if user_key in self._config_cache:
            del self._config_cache[user_key]
            self._save_config()
            return "Đã reset cấu hình về mặc định"
        return "Không có cấu hình để reset"

# Singleton instance
_user_config_manager = None

def get_user_config_manager() -> UserConfigManager:
    """Lấy instance của UserConfigManager (singleton pattern)"""
    global _user_config_manager
    if _user_config_manager is None:
        _user_config_manager = UserConfigManager()
    return _user_config_manager