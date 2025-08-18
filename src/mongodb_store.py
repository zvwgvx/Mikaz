#!/usr/bin/env python3
# coding: utf-8
# mongodb_store.py - MongoDB storage for user configs, memory, and authorized users

import logging
from typing import Dict, List, Optional, Any, Set
from datetime import datetime
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
import tiktoken

logger = logging.getLogger("discord-openai-proxy.mongodb_store")

class MongoDBStore:
    """MongoDB storage manager for Discord OpenAI proxy"""
    
    def __init__(self, connection_string: str, database_name: str = "discord_openai_proxy"):
        self.connection_string = connection_string
        self.database_name = database_name
        self.client: Optional[MongoClient] = None
        self.db = None
        self.tokenizer = tiktoken.encoding_for_model("gpt-4")
        
        # Collection names
        self.COLLECTIONS = {
            'user_config': 'user_configs',
            'memory': 'user_memory', 
            'authorized': 'authorized_users'
        }
        
        self._connect()
    
    def _connect(self):
        """Establish MongoDB connection"""
        try:
            self.client = MongoClient(
                self.connection_string,
                serverSelectionTimeoutMS=5000,  # 5 second timeout
                connectTimeoutMS=5000,
                socketTimeoutMS=5000
            )
            
            # Test connection
            self.client.admin.command('ping')
            self.db = self.client[self.database_name]
            
            # Create indexes for better performance
            self._create_indexes()
            
            logger.info(f"Successfully connected to MongoDB: {self.database_name}")
            
        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error connecting to MongoDB: {e}")
            raise
    
    def _create_indexes(self):
        """Create necessary indexes"""
        try:
            # User config indexes
            self.db[self.COLLECTIONS['user_config']].create_index("user_id", unique=True)
            
            # Memory indexes
            self.db[self.COLLECTIONS['memory']].create_index("user_id", unique=True)
            
            # Authorized users indexes
            self.db[self.COLLECTIONS['authorized']].create_index("user_id", unique=True)
            
            logger.info("MongoDB indexes created successfully")
        except Exception as e:
            logger.exception(f"Error creating indexes: {e}")
    
    def close(self):
        """Close MongoDB connection"""
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed")
    
    # =====================================
    # USER CONFIG METHODS
    # =====================================
    
    def get_user_config(self, user_id: int) -> Dict[str, Any]:
        """Get user configuration"""
        try:
            result = self.db[self.COLLECTIONS['user_config']].find_one({"user_id": user_id})
            if result:
                return {
                    "model": result.get("model", "gpt-5"),
                    "system_prompt": result.get("system_prompt", "Bạn là một trợ lý AI thông minh.")
                }
            else:
                # Return default config
                return {
                    "model": "gpt-5", 
                    "system_prompt": "Bạn là một trợ lý AI thông minh."
                }
        except Exception as e:
            logger.exception(f"Error getting user config for {user_id}: {e}")
            return {"model": "gpt-5", "system_prompt": "Bạn là một trợ lý AI thông minh."}
    
    def set_user_config(self, user_id: int, model: Optional[str] = None, system_prompt: Optional[str] = None) -> bool:
        """Set user configuration"""
        try:
            update_data = {"updated_at": datetime.utcnow()}
            if model is not None:
                update_data["model"] = model
            if system_prompt is not None:
                update_data["system_prompt"] = system_prompt
            
            result = self.db[self.COLLECTIONS['user_config']].update_one(
                {"user_id": user_id},
                {
                    "$set": update_data,
                    "$setOnInsert": {
                        "user_id": user_id,
                        "created_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
            return True
        except Exception as e:
            logger.exception(f"Error setting user config for {user_id}: {e}")
            return False
    
    def get_user_model(self, user_id: int) -> str:
        """Get user's preferred model"""
        config = self.get_user_config(user_id)
        return config["model"]
    
    def get_user_system_prompt(self, user_id: int) -> str:
        """Get user's system prompt"""
        config = self.get_user_config(user_id)
        return config["system_prompt"]
    
    def get_user_system_message(self, user_id: int) -> Dict[str, str]:
        """Get system message in OpenAI format"""
        return {
            "role": "system",
            "content": self.get_user_system_prompt(user_id)
        }
    
    # =====================================
    # MEMORY METHODS
    # =====================================
    
    def get_user_messages(self, user_id: int) -> List[Dict[str, str]]:
        """Get user's conversation history"""
        try:
            result = self.db[self.COLLECTIONS['memory']].find_one({"user_id": user_id})
            if result and "messages" in result:
                return result["messages"]
            return []
        except Exception as e:
            logger.exception(f"Error getting messages for user {user_id}: {e}")
            return []
    
    def add_message(self, user_id: int, message: Dict[str, str], max_messages: int = 50, max_tokens: int = 2000):
        """Add message to user's conversation history"""
        try:
            # Get current messages
            current_messages = self.get_user_messages(user_id)
            current_messages.append(message)
            
            # Prune messages if needed
            current_messages = self._prune_messages(current_messages, max_messages, max_tokens)
            
            # Update in database
            result = self.db[self.COLLECTIONS['memory']].update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "messages": current_messages,
                        "updated_at": datetime.utcnow()
                    },
                    "$setOnInsert": {
                        "user_id": user_id,
                        "created_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
            return True
        except Exception as e:
            logger.exception(f"Error adding message for user {user_id}: {e}")
            return False
    
    def clear_user_memory(self, user_id: int) -> bool:
        """Clear user's conversation history"""
        try:
            result = self.db[self.COLLECTIONS['memory']].delete_one({"user_id": user_id})
            return result.deleted_count > 0
        except Exception as e:
            logger.exception(f"Error clearing memory for user {user_id}: {e}")
            return False
    
    def _prune_messages(self, messages: List[Dict[str, str]], max_messages: int, max_tokens: int) -> List[Dict[str, str]]:
        """Prune messages based on count and token limits"""
        # Remove oldest messages if over limit
        while len(messages) > max_messages:
            messages.pop(0)
        
        # Remove oldest messages if over token limit
        total_tokens = sum(len(self.tokenizer.encode(msg["content"])) for msg in messages)
        while total_tokens > max_tokens and messages:
            removed = messages.pop(0)
            total_tokens -= len(self.tokenizer.encode(removed["content"]))
        
        return messages
    
    # =====================================
    # AUTHORIZED USERS METHODS
    # =====================================
    
    def get_authorized_users(self) -> Set[int]:
        """Get set of authorized user IDs"""
        try:
            results = self.db[self.COLLECTIONS['authorized']].find({}, {"user_id": 1})
            return {doc["user_id"] for doc in results}
        except Exception as e:
            logger.exception(f"Error getting authorized users: {e}")
            return set()
    
    def add_authorized_user(self, user_id: int) -> bool:
        """Add user to authorized list"""
        try:
            result = self.db[self.COLLECTIONS['authorized']].update_one(
                {"user_id": user_id},
                {
                    "$setOnInsert": {
                        "user_id": user_id,
                        "created_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
            return True
        except Exception as e:
            logger.exception(f"Error adding authorized user {user_id}: {e}")
            return False
    
    def remove_authorized_user(self, user_id: int) -> bool:
        """Remove user from authorized list"""
        try:
            result = self.db[self.COLLECTIONS['authorized']].delete_one({"user_id": user_id})
            return result.deleted_count > 0
        except Exception as e:
            logger.exception(f"Error removing authorized user {user_id}: {e}")
            return False
    
    def is_user_authorized(self, user_id: int) -> bool:
        """Check if user is authorized"""
        try:
            result = self.db[self.COLLECTIONS['authorized']].find_one({"user_id": user_id})
            return result is not None
        except Exception as e:
            logger.exception(f"Error checking authorization for user {user_id}: {e}")
            return False

# Singleton instance
_mongodb_store: Optional[MongoDBStore] = None

def get_mongodb_store() -> MongoDBStore:
    """Get singleton MongoDB store instance"""
    global _mongodb_store
    if _mongodb_store is None:
        raise RuntimeError("MongoDB store not initialized. Call init_mongodb_store() first.")
    return _mongodb_store

def init_mongodb_store(connection_string: str, database_name: str = "discord_openai_proxy") -> MongoDBStore:
    """Initialize MongoDB store singleton"""
    global _mongodb_store
    if _mongodb_store is None:
        _mongodb_store = MongoDBStore(connection_string, database_name)
    return _mongodb_store

def close_mongodb_store():
    """Close MongoDB store connection"""
    global _mongodb_store
    if _mongodb_store:
        _mongodb_store.close()
        _mongodb_store = None