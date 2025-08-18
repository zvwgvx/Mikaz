#!/usr/bin/env python3
# coding: utf-8
# ────────────────────────────────────────────────────────────────────────
# Bot helper / command registry
# Uses MemoryStore for per‑user conversation history
# Uses UserConfigManager for per-user model and system prompt settings
# ────────────────────────────────────────────────────────────────────────

import re
import json
import logging
import asyncio
from pathlib import Path
from typing import Set, Optional, List, Dict

import discord
from discord.ext import commands

# ───────────────────────────────────────────────────────
# ***Absolute import – no package, so we use the plain module name ***
from memory_store import MemoryStore
from user_config import get_user_config_manager
from request_queue import get_request_queue

logger = logging.getLogger("discord-openai-proxy.functions")

# ---------------------------- module‑level state -----------------------------
_bot: Optional[commands.Bot] = None
_call_api = None
_config = None
_user_config_manager = None
_request_queue = None

# ---------------------------------------------------------------
# Persistence helpers – authorized user IDs
# ---------------------------------------------------------------
_authorized_users: Set[int] = set()

# ---------------------------------------------------------------
# Attachment handling constants
# ---------------------------------------------------------------
FILE_MAX_BYTES = 200 * 1024          # 200 KB per file
MAX_CHARS_PER_FILE = 10_000
ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".java", ".c", ".cpp", ".h",
    ".json", ".yaml", ".yml", ".csv", ".rs", ".go", ".rb",
    ".sh", ".html", ".css", ".ts", ".ini", ".toml",
}

# ---------------------------------------------------------------
# Optional memory store
# ---------------------------------------------------------------
_memory_store: Optional[MemoryStore] = None

# ------------------------------------------------------------------
# Persistence helpers – authorized users
# ------------------------------------------------------------------

def load_authorized_from_path(path: Path) -> Set[int]:
    """Load authorized users from file (legacy mode)"""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            arr = data.get("authorized", [])
            return set(int(x) for x in arr)
        except Exception:
            logger.exception("Failed to load authorized.json, returning empty set.")
    return set()
def save_authorized_to_path(path: Path, s: Set[int]) -> None:
    """Save authorized users to file (legacy mode)"""
    try:
        path.write_text(json.dumps({"authorized": sorted(list(s))}, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save authorized.json")

def load_authorized_users() -> Set[int]:
    """Load authorized users from storage backend"""
    global _use_mongodb_auth, _mongodb_store
    
    if _use_mongodb_auth and _mongodb_store:
        return _mongodb_store.get_authorized_users()
    else:
        return load_authorized_from_path(_config.AUTHORIZED_STORE)
    
def add_authorized_user(user_id: int) -> bool:
    """Add user to authorized list"""
    global _authorized_users, _use_mongodb_auth, _mongodb_store
    
    if _use_mongodb_auth and _mongodb_store:
        success = _mongodb_store.add_authorized_user(user_id)
        if success:
            _authorized_users.add(user_id)
        return success
    else:
        _authorized_users.add(user_id)
        save_authorized_to_path(_config.AUTHORIZED_STORE, _authorized_users)
        return True
    
def remove_authorized_user(user_id: int) -> bool:
    """Remove user from authorized list"""
    global _authorized_users, _use_mongodb_auth, _mongodb_store
    
    if _use_mongodb_auth and _mongodb_store:
        success = _mongodb_store.remove_authorized_user(user_id)
        if success:
            _authorized_users.discard(user_id)
        return success
    else:
        if user_id in _authorized_users:
            _authorized_users.remove(user_id)
            save_authorized_to_path(_config.AUTHORIZED_STORE, _authorized_users)
            return True
        return False
    
# ------------------------------------------------------------------
# Utility helpers
# ------------------------------------------------------------------
async def is_authorized_user(user: discord.abc.User) -> bool:
    """Return True if `user` is the bot owner or in the authorized set."""
    global _bot, _authorized_users
    try:
        if await _bot.is_owner(user):
            return True
    except Exception:
        pass
    return getattr(user, "id", None) in _authorized_users


def _extract_user_id_from_str(s: str) -> Optional[int]:
    m = re.search(r"(\d{17,20})", s)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return None
    return None


def should_respond_default(message: discord.Message) -> bool:
    """Return True for a DM or an explicit mention of the bot."""
    if isinstance(message.channel, discord.DMChannel):
        return True
    if _bot.user in message.mentions:
        return True
    return False

# ------------------------------------------------------------------
# Attachment helpers
# ------------------------------------------------------------------
async def _read_attachments_as_text(attachments: List[discord.Attachment]) -> List[Dict]:
    """Return a list of dicts describing each attachment that looks like text."""
    result = []
    for att in attachments:
        entry = {"filename": att.filename, "text": "", "skipped": False, "reason": None}

        # quick size check
        try:
            size = int(getattr(att, "size", 0) or 0)
        except Exception:
            size = 0

        ext = (Path(att.filename).suffix or "").lower()
        content_type = getattr(att, "content_type", "") or ""

        # filter by content‑type / extension
        if not (
            content_type.startswith("text")
            or content_type in ("application/json", "application/javascript")
            or ext in ALLOWED_EXTENSIONS
        ):
            entry["skipped"] = True
            entry["reason"] = f"unsupported file type ({content_type!r}, {ext!r})"
            result.append(entry)
            continue

        if size and size > FILE_MAX_BYTES:
            entry["skipped"] = True
            entry["reason"] = f"file too large ({size} bytes)"
            result.append(entry)
            continue

        try:
            b = await att.read()
            try:
                text = b.decode("utf-8")
            except Exception:
                try:
                    text = b.decode("latin-1")
                except Exception:
                    text = b.decode("utf-8", errors="replace")

            # truncate very long files
            if len(text) > MAX_CHARS_PER_FILE:
                text = text[:MAX_CHARS_PER_FILE] + "\n\n...[truncated]..."

            entry["text"] = text
            result.append(entry)
        except Exception as e:
            logger.exception("Error reading attachment %s", att.filename)
            entry["skipped"] = True
            entry["reason"] = f"read error: {e}"
            result.append(entry)

    return result

# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------
async def help_cmd(ctx: commands.Context):
    is_owner = False
    try:
        is_owner = await _bot.is_owner(ctx.author)
    except Exception:
        pass

    lines = [
        "**Available commands:**",
        "`;getid [@member]` – Show your ID (or a mention). (everyone)",
        "`;ping` – Check bot responsiveness. (everyone)",
        "`;setmodel <model>` – Set your preferred AI model. (authorized users)",
        "`;setsprompt <prompt>` – Set your system prompt. (authorized users)", 
        "`;showconfig` – Show your current model and system prompt. (authorized users)",
        "",
        "**Supported models:** gpt-oss-120b, gpt-oss-20b, gpt-5, o3-mini, gpt-4.1",
        "",
        "**Attachment support:** when you attach a text/code file (.py, .txt, .md, .json, …)",
        "the bot will read it and include its contents in the reply.",
    ]

    if is_owner:
        lines += [
            "",
            "**Owner‑only commands:**",
            "`;addid <id|@mention>` – Add a user to `authorized.json`.",
            "`;removeid <id|@mention>` – Remove user from `authorized.json`.",
            "`;listauth` – List authorized IDs.",
            "`;memory` – View your conversation history.",
            "`;clearmemory [@user]` – Clear a conversation history.",
            "",
        ]

    await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


async def getid_cmd(ctx: commands.Context, member: discord.Member = None):
    if member is None:
        await ctx.send(f"Your ID: {ctx.author.id}", allowed_mentions=discord.AllowedMentions.none())
    else:
        await ctx.send(f"{member} ID: {member.id}", allowed_mentions=discord.AllowedMentions.none())

async def addid_cmd(ctx: commands.Context, id_or_mention: str):
    global _authorized_users
    uid = _extract_user_id_from_str(id_or_mention)
    if uid is None:
        await ctx.send("Invalid parameter. Provide a user ID or a mention.", allowed_mentions=discord.AllowedMentions.none())
        return

    if uid in _authorized_users:
        await ctx.send(f"ID {uid} is already authorized.", allowed_mentions=discord.AllowedMentions.none())
        return

    success = add_authorized_user(uid)
    if success:
        await ctx.send(f"Added ID {uid} to authorized list.", allowed_mentions=discord.AllowedMentions.none())
    else:
        await ctx.send(f"Failed to add ID {uid} to authorized list.", allowed_mentions=discord.AllowedMentions.none())

async def removeid_cmd(ctx: commands.Context, id_or_mention: str):
    global _authorized_users
    uid = _extract_user_id_from_str(id_or_mention)
    if uid is None:
        await ctx.send("Invalid parameter. Provide a user ID or a mention.", allowed_mentions=discord.AllowedMentions.none())
        return

    if uid not in _authorized_users:
        await ctx.send(f"ID {uid} is not in the authorized list.", allowed_mentions=discord.AllowedMentions.none())
        return

    success = remove_authorized_user(uid)
    if success:
        await ctx.send(f"Removed ID {uid} from authorized list.", allowed_mentions=discord.AllowedMentions.none())
    else:
        await ctx.send(f"Failed to remove ID {uid} from authorized list.", allowed_mentions=discord.AllowedMentions.none())

async def listauth_cmd(ctx: commands.Context):
    if not _authorized_users:
        await ctx.send("Authorized list is empty.", allowed_mentions=discord.AllowedMentions.none())
        return

    body = "\n".join(str(x) for x in sorted(_authorized_users))
    if len(body) > 1900:
        # For MongoDB mode, create a temporary text with the list
        if _use_mongodb_auth:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                f.write("Authorized Users:\n")
                f.write(body)
                temp_path = f.name
            
            try:
                await ctx.send("List too long, sending as file.", 
                             allowed_mentions=discord.AllowedMentions.none(),
                             file=discord.File(temp_path, filename="authorized_users.txt"))
            finally:
                Path(temp_path).unlink(missing_ok=True)
        else:
            # File mode - use existing file
            fp = _config.AUTHORIZED_STORE if _config.AUTHORIZED_STORE.exists() else None
            if fp:
                await ctx.send("List too long, sending authorized.json file.", 
                             allowed_mentions=discord.AllowedMentions.none(), 
                             file=discord.File(fp))
            else:
                await ctx.send("List too long, authorized.json not found.", 
                             allowed_mentions=discord.AllowedMentions.none())
    else:
        await ctx.send(f"Authorized IDs:\n{body}", allowed_mentions=discord.AllowedMentions.none())

async def ping_cmd(ctx: commands.Context):
    import time
    start_time = time.perf_counter()
    message = await ctx.send("Pinging...", allowed_mentions=discord.AllowedMentions.none())
    end_time = time.perf_counter()
    
    # Tính thời gian phản hồi (ms)
    latency_ms = round((end_time - start_time) * 1000)
    
    # Lấy WebSocket latency của bot (nếu có)
    ws_latency = round(_bot.latency * 1000) if _bot.latency else "N/A"
    
    # Cập nhật tin nhắn với thông tin chi tiết
    content = f"Pong! \nResponse: {latency_ms} ms\nWebSocket: {ws_latency} ms"
    await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())

# ------------------------------------------------------------------
# Owner‑only memory commands
# ------------------------------------------------------------------
async def memory_cmd(ctx: commands.Context, member: discord.Member = None):
    """View the conversation history of *member* (or the author)."""
    target = member or ctx.author
    if _memory_store is None:
        await ctx.send("Memory feature not initialized.", allowed_mentions=discord.AllowedMentions.none())
        return

    mem = _memory_store.get_user_messages(target.id)
    if not mem:
        await ctx.send(f"No memory for {target}.", allowed_mentions=discord.AllowedMentions.none())
        return

    lines = []
    for i, msg in enumerate(mem[-10:], start=1):
        content = msg["content"]
        preview = (content[:120] + "…") if len(content) > 120 else content
        lines.append(f"{i:02d}. **{msg['role']}**: {preview}")

    await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


async def clearmemory_cmd(ctx: commands.Context, target: discord.Member = None):
    """Owner‑only: delete the conversation history of *target* (or the author)."""
    target = target or ctx.author
    if _memory_store is None:
        await ctx.send("Memory feature not initialized.", allowed_mentions=discord.AllowedMentions.none())
        return

    _memory_store.clear_user(target.id)
    await ctx.send(f"Cleared memory for {target}.", allowed_mentions=discord.AllowedMentions.none())

# ------------------------------------------------------------------
# User configuration commands
# ------------------------------------------------------------------
async def setmodel_cmd(ctx: commands.Context, *, model: str = None):
    """Set user's preferred AI model."""
    # Check authorization
    if not await is_authorized_user(ctx.author):
        await ctx.send("Bạn không có quyền sử dụng lệnh này.", allowed_mentions=discord.AllowedMentions.none())
        return
    
    if model is None:
        from user_config import SUPPORTED_MODELS
        supported_list = ", ".join(sorted(SUPPORTED_MODELS))
        await ctx.send(f"Vui lòng chỉ định model. Ví dụ: `;setmodel gpt-oss-120b`\n**Các model có sẵn:** {supported_list}", 
                      allowed_mentions=discord.AllowedMentions.none())
        return
    
    model = model.strip()
    success, message = _user_config_manager.set_user_model(ctx.author.id, model)
    await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())


async def setsprompt_cmd(ctx: commands.Context, *, prompt: str = None):
    """Set user's system prompt."""
    # Check authorization
    if not await is_authorized_user(ctx.author):
        await ctx.send("Bạn không có quyền sử dụng lệnh này.", allowed_mentions=discord.AllowedMentions.none())
        return
    
    if prompt is None:
        await ctx.send("Vui lòng cung cấp system prompt. Ví dụ: `;setsprompt Bạn là một trợ lý AI thông minh`", 
                      allowed_mentions=discord.AllowedMentions.none())
        return
    
    success, message = _user_config_manager.set_user_system_prompt(ctx.author.id, prompt)
    await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())


async def showconfig_cmd(ctx: commands.Context):
    """Show user's current configuration."""
    # Check authorization
    if not await is_authorized_user(ctx.author):
        await ctx.send("Bạn không có quyền sử dụng lệnh này.", allowed_mentions=discord.AllowedMentions.none())
        return
    
    user_config = _user_config_manager.get_user_config(ctx.author.id)
    
    model = user_config["model"]
    prompt = user_config["system_prompt"]
    
    # Truncate prompt if too long for display
    display_prompt = prompt
    if len(prompt) > 500:
        display_prompt = prompt[:500] + "...[truncated]"
    
    lines = [
        "**Cấu hình hiện tại của bạn:**",
        f"**Model:** `{model}`",
        f"**System Prompt:**",
        f"```",
        display_prompt,
        f"```"
    ]
    
    await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())
    
# ------------------------------------------------------------------
# AI Request Processing Function (used by queue)
# ------------------------------------------------------------------
async def process_ai_request(request):
    """Process a single AI request from the queue"""
    message = request.message
    final_user_text = request.final_user_text
    
    # Store the user's message in memory first (before API call)
    if _memory_store:
        _memory_store.add_message(message.author.id, {"role": "user", "content": final_user_text})

    # Build payload for OpenAI với model và system prompt riêng của user
    user_system_message = _user_config_manager.get_user_system_message(message.author.id)
    user_model = _user_config_manager.get_user_model(message.author.id)
    
    user_memory = _memory_store.get_user_messages(message.author.id) if _memory_store else []
    payload_messages = [user_system_message] + user_memory + [{"role": "user", "content": final_user_text}]

    # ------------------------------------------------------------------
    # Call OpenAI với model riêng của user
    # ------------------------------------------------------------------
    try:
        # Send typing indicator
        async with message.channel.typing():
            loop = asyncio.get_running_loop()
            ok, resp = await loop.run_in_executor(None, _call_api.call_openai_proxy, payload_messages, user_model)
    except Exception as e:
        logger.exception("Error calling openai proxy async")
        await message.channel.send(
            f"❌ Internal error: {e}",
            reference=message,
            allowed_mentions=discord.AllowedMentions.none()
        )
        return

    if not ok:
        await message.channel.send(
            f"❌ [OpenAI PROXY ERROR] {resp}",
            reference=message,
            allowed_mentions=discord.AllowedMentions.none()
        )
        return

    reply = (resp or "").strip() or "(no response from AI)"

    # ------------------------------------------------------------------
    # Convert LaTeX symbols to Discord-friendly format
    # ------------------------------------------------------------------
    reply = convert_latex_to_discord(reply)

    # ------------------------------------------------------------------
    # Store assistant reply in memory
    # ------------------------------------------------------------------
    if _memory_store:
        _memory_store.add_message(message.author.id, {"role": "assistant", "content": reply})

    # ------------------------------------------------------------------
    # Send reply to Discord with reference to original message
    # ------------------------------------------------------------------
    try:
        await send_long_message_with_reference(message.channel, reply, message, _config.MAX_MSG)
    except Exception:
        logger.exception("Error sending reply to Discord")


# ------------------------------------------------------------------
# Fixed Message formatting helpers with proper table handling
# ------------------------------------------------------------------

def convert_latex_to_discord(text: str) -> str:
    """
    Fixed version - only protect code blocks, NOT markdown tables
    """
    
    # Step 1: Only protect code regions, NOT tables
    protected_regions = []
    
    def protect_region(match):
        content = match.group(0)
        placeholder = f"__PROTECTED_{len(protected_regions)}__"
        protected_regions.append(content)
        return placeholder
    
    # Only protect code-related patterns - DO NOT protect tables
    patterns_to_protect = [
        r'```[\s\S]*?```',  # Code blocks
        r'`[^`\n]*?`',      # Inline code only
        # Programming patterns (but not tables!)
        r'#include\s*<[^>]+>', # C++ includes
        r'\b(?:cout|cin|std::)\b[^.\n]*?;',  # C++ statements
        r'\bfor\s*\([^)]*\)\s*\{[^}]*\}',   # For loops
        r'\bwhile\s*\([^)]*\)\s*\{[^}]*\}', # While loops
        r'\bif\s*\([^)]*\)\s*\{[^}]*\}',    # If statements
    ]
    
    working_text = text
    for pattern in patterns_to_protect:
        working_text = re.sub(pattern, protect_region, working_text, flags=re.MULTILINE | re.DOTALL)
    
    # Step 2: Apply LaTeX conversion to remaining text (including tables)
    # Simple replacements for common LaTeX symbols
    latex_replacements = {
        r'\\cdot\b': '·', r'\\times\b': '×', r'\\div\b': '÷', r'\\pm\b': '±',
        r'\\leq\b': '≤', r'\\geq\b': '≥', r'\\neq\b': '≠', r'\\approx\b': '≈',
        r'\\alpha\b': 'α', r'\\beta\b': 'β', r'\\gamma\b': 'γ', r'\\delta\b': 'δ',
        r'\\pi\b': 'π', r'\\sigma\b': 'σ', r'\\lambda\b': 'λ', r'\\mu\b': 'μ',
        r'\\rightarrow\b': '→', r'\\to\b': '→', r'\\leftarrow\b': '←',
        r'\\sum\b': 'Σ', r'\\prod\b': 'Π', r'\\int\b': '∫',
        r'\\infty\b': '∞', r'\\emptyset\b': '∅',
    }
    
    for latex_pattern, replacement in latex_replacements.items():
        working_text = re.sub(latex_pattern, replacement, working_text)
    
    # Handle fractions \frac{a}{b} -> a/b
    def replace_fraction(match):
        numerator = match.group(1).strip()
        denominator = match.group(2).strip()
        if len(numerator) <= 3 and len(denominator) <= 3:
            return f'{numerator}/{denominator}'
        else:
            return f'({numerator})/({denominator})'
    
    working_text = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', replace_fraction, working_text)
    
    # Step 3: Restore protected regions
    for i, protected_content in enumerate(protected_regions):
        placeholder = f"__PROTECTED_{i}__"
        working_text = working_text.replace(placeholder, protected_content)
    
    return working_text

def is_table_line(line: str) -> bool:
    """Check if a line is part of a markdown table"""
    stripped = line.strip()
    # Table data line: starts and ends with |, has at least 2 |
    if stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 2:
        return True
    # Table separator line: |---|---| or |:---|---:| etc
    if (stripped.startswith('|') and 
        all(c in '|-: \t' for c in stripped) and
        '-' in stripped and stripped.count('|') >= 2):
        return True
    return False

def find_complete_table(lines: list, start_idx: int) -> tuple:
    """Find the complete table boundaries starting from any table line"""
    if start_idx >= len(lines) or not is_table_line(lines[start_idx]):
        return start_idx, start_idx
    
    # Find table start (go backwards)
    table_start = start_idx
    while table_start > 0 and is_table_line(lines[table_start - 1]):
        table_start -= 1
    
    # Find table end (go forwards)
    table_end = start_idx
    while table_end < len(lines) - 1 and is_table_line(lines[table_end + 1]):
        table_end += 1
    
    return table_start, table_end

def split_message_smart(text: str, max_length: int = 2000) -> list[str]:
    """
    Smart message splitting that keeps tables intact
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current_chunk = ""
    in_code_block = False
    code_block_lang = ""
    
    lines = text.split('\n')
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # Handle code blocks
        code_match = re.match(r'^```(\w*)', line.strip())
        if code_match:
            if not in_code_block:
                in_code_block = True
                code_block_lang = code_match.group(1)
            else:
                in_code_block = False
                code_block_lang = ""
        
        # Handle tables (only when NOT in code block)
        if is_table_line(line) and not in_code_block:
            table_start, table_end = find_complete_table(lines, i)
            
            # Get the entire table as one unit
            table_lines = lines[table_start:table_end + 1]
            table_text = '\n'.join(table_lines)
            
            # Try to add the complete table to current chunk
            test_chunk = current_chunk + ('\n' if current_chunk else '') + table_text
            
            if len(test_chunk) <= max_length:
                # Table fits in current chunk
                current_chunk = test_chunk
            else:
                # Table doesn't fit
                if current_chunk:
                    # Save current chunk first
                    if in_code_block:
                        current_chunk += '\n```'
                    chunks.append(current_chunk)
                    if in_code_block:
                        current_chunk = f'```{code_block_lang}'
                    else:
                        current_chunk = ""
                
                # Handle the table
                if len(table_text) <= max_length:
                    # Table fits in its own chunk
                    current_chunk = table_text
                else:
                    # Table is too large - need to split it intelligently
                    # Keep header + separator together if possible
                    header_lines = []
                    data_lines = []
                    
                    # Try to identify header vs data
                    for j, tline in enumerate(table_lines):
                        if j < 2:  # Usually header + separator
                            header_lines.append(tline)
                        else:
                            data_lines.append(tline)
                    
                    if len(header_lines) >= 2:
                        header_text = '\n'.join(header_lines)
                        if len(header_text) <= max_length:
                            # Start with header
                            current_table_chunk = header_text
                            
                            # Add data lines one by one
                            for data_line in data_lines:
                                test_line = current_table_chunk + '\n' + data_line
                                if len(test_line) <= max_length:
                                    current_table_chunk = test_line
                                else:
                                    # Current chunk is full, save it
                                    chunks.append(current_table_chunk)
                                    # Start new chunk with header + current data line
                                    current_table_chunk = header_text + '\n' + data_line
                            
                            current_chunk = current_table_chunk
                        else:
                            # Even header is too long, fallback to line by line
                            for tline in table_lines:
                                test_line = current_chunk + ('\n' if current_chunk else '') + tline
                                if len(test_line) <= max_length:
                                    current_chunk = test_line
                                else:
                                    if current_chunk:
                                        chunks.append(current_chunk)
                                    current_chunk = tline
                    else:
                        # Fallback: process line by line
                        for tline in table_lines:
                            test_line = current_chunk + ('\n' if current_chunk else '') + tline
                            if len(test_line) <= max_length:
                                current_chunk = test_line
                            else:
                                if current_chunk:
                                    chunks.append(current_chunk)
                                current_chunk = tline
            
            # Skip to after the table
            i = table_end + 1
            continue
        
        # Regular line processing (not part of a table)
        test_chunk = current_chunk + ('\n' if current_chunk else '') + line
        
        if len(test_chunk) > max_length:
            if current_chunk:
                # Save current chunk
                if in_code_block:
                    current_chunk += '\n```'
                    chunks.append(current_chunk)
                    current_chunk = f'```{code_block_lang}\n{line}'
                else:
                    chunks.append(current_chunk)
                    current_chunk = line
            else:
                # Single line is too long - split it
                if len(line) > max_length:
                    # Preserve indentation
                    leading_whitespace = re.match(r'^(\s*)', line).group(1)
                    line_content = line[len(leading_whitespace):]
                    
                    while len(line_content) > max_length - len(leading_whitespace):
                        available_space = max_length - len(leading_whitespace)
                        part_content = line_content[:available_space]
                        chunks.append(leading_whitespace + part_content)
                        line_content = line_content[available_space:]
                    
                    if line_content:
                        current_chunk = leading_whitespace + line_content
                else:
                    current_chunk = line
        else:
            current_chunk = test_chunk
        
        i += 1
    
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks

# Update the message sending functions
async def send_long_message(channel, content: str, max_msg_length: int = 2000):
    """
    Send long message with proper table handling
    """
    # First apply LaTeX conversion (which now preserves tables)
    formatted_content = convert_latex_to_discord(content)
    
    if len(formatted_content) <= max_msg_length:
        await channel.send(formatted_content, allowed_mentions=discord.AllowedMentions.none())
        return
    
    chunks = split_message_smart(formatted_content, max_msg_length)
    
    for i, chunk in enumerate(chunks):
        if i > 0:  # Add delay between messages
            await asyncio.sleep(0.3)
        await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())


async def send_long_message_with_reference(channel, content: str, reference_message: discord.Message, max_msg_length: int = 2000):
    """
    Send long message with reference and proper table handling
    """
    # First apply LaTeX conversion (which now preserves tables)
    formatted_content = convert_latex_to_discord(content)
    
    if len(formatted_content) <= max_msg_length:
        await channel.send(
            formatted_content,
            reference=reference_message,
            allowed_mentions=discord.AllowedMentions.none()
        )
        return
    
    chunks = split_message_smart(formatted_content, max_msg_length)
    
    for i, chunk in enumerate(chunks):
        if i > 0:  # Add delay between messages
            await asyncio.sleep(0.3)
        
        # Only reference the original message for the first chunk
        ref = reference_message if i == 0 else None
        await channel.send(
            chunk,
            reference=ref,
            allowed_mentions=discord.AllowedMentions.none()
        )

# ------------------------------------------------------------------
# on_message listener – central dispatch point
# ------------------------------------------------------------------
async def on_message(message: discord.Message):
    try:
        logger.info(
            "on_message invoked: func id=%s module=%s qualname=%s author=%s content=%s",
            hex(id(on_message)),
            on_message.__module__,
            getattr(on_message, "__qualname__", "?"),
            f"{message.author}({getattr(message.author, 'id', None)})",
            (message.content or "")[:120],
        )
    except Exception:
        pass

    if message.author.bot:
        return

    content = (message.content or "").strip()

    # 1️⃣ FIXED: Commands that start with prefix - DON'T manually process them here
    # Discord.py will automatically handle them via the command framework
    if content.startswith(";"):
        # Just return, let discord.py handle the command processing
        return

    # 2️⃣ Default trigger (DM or mention) - for AI responses
    authorized = await is_authorized_user(message.author)
    attachments = list(message.attachments or [])

    if not should_respond_default(message):
        # Not a DM or mention, let discord.py process any commands if present
        return

    if not authorized:
        try:
            await message.channel.send("You do not have permission to use this bot.", allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("Failed to send unauthorized message")
        return

    # ------------------------------------------------------------------
    # Build the user prompt (after stripping the bot mention)
    # ------------------------------------------------------------------
    user_text = content
    if _bot.user in message.mentions:
        user_text = re.sub(rf"<@!?{_bot.user.id}>", "", content).strip()

    # ------------------------------------------------------------------
    # Handle attachments
    # ------------------------------------------------------------------
    attachment_text = ""
    if attachments:
        files_info = await _read_attachments_as_text(attachments)
        attach_summary = []
        for fi in files_info:
            if fi.get("skipped"):
                attach_summary.append(f"- {fi['filename']}: SKIPPED ({fi.get('reason')})")
            else:
                attach_summary.append(f"- {fi['filename']}: included ({len(fi['text'])} chars)")
        header = "\n".join(attach_summary) + "\n\n"

        files_combined = ""
        for fi in files_info:
            if not fi.get("skipped"):
                files_combined += f"Filename: {fi['filename']}\n---\n{fi['text']}\n\n"
        attachment_text = header + files_combined

    final_user_text = (attachment_text + user_text).strip()
    if not final_user_text:
        await message.channel.send(
            "Please send a message (mention me or DM me) with your question. "
            "Example: mention me and ask 'explain Dijkstra algorithm briefly'.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # ------------------------------------------------------------------
    # Add request to queue instead of processing directly
    # ------------------------------------------------------------------
    try:
        success, status_message = await _request_queue.add_request(message, final_user_text)
        if not success:
            await message.channel.send(status_message, allowed_mentions=discord.AllowedMentions.none())
            return
        
        # Send status message if not immediately processing
        queue_size = _request_queue._queue.qsize()
        processing_count = len(_request_queue._processing_users)
        is_owner = await _request_queue.is_owner(message.author)
        
        if queue_size > 1 or processing_count > 0:
            await message.channel.send(
                status_message,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none()
            )
    
    except Exception as e:
        logger.exception("Error adding request to queue")
        await message.channel.send(
            f"❌ Error adding request to queue: {e}",
            allowed_mentions=discord.AllowedMentions.none()
        )

# ------------------------------------------------------------------
# Setup – register commands, listeners, load data
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# Setup – register commands, listeners, load data (updated for MongoDB)
# ------------------------------------------------------------------
def setup(bot: commands.Bot, call_api_module, config_module):
    global _bot, _call_api, _config, _authorized_users, _memory_store, _user_config_manager, _request_queue
    global _use_mongodb_auth, _mongodb_store

    _bot = bot
    _call_api = call_api_module
    _config = config_module
    
    # Initialize storage backend
    _config.init_storage()
    
    # Check if we're using MongoDB
    _use_mongodb_auth = _config.USE_MONGODB
    if _use_mongodb_auth:
        from mongodb_store import get_mongodb_store
        _mongodb_store = get_mongodb_store()
        logger.info("Using MongoDB for data storage")
    else:
        _mongodb_store = None
        logger.info("Using file-based storage (legacy mode)")
    
    # Initialize managers
    _user_config_manager = get_user_config_manager()
    _request_queue = get_request_queue()

    # Setup queue
    _request_queue.set_bot(bot)
    _request_queue.set_process_callback(process_ai_request)

    # Load authorized users
    _authorized_users = load_authorized_users()
    logger.info("Functions module initialized. Authorized users: %s", sorted(_authorized_users))

    # Initialize memory store
    _memory_store = MemoryStore()
    if not _use_mongodb_auth:
        logger.info("Memory store: %d users cached", len(_memory_store._cache))
    else:
        logger.info("Memory store initialized with MongoDB backend")

    # ------------------------------------------------------------------
    # Remove default help (if any)
    # ------------------------------------------------------------------
    try:
        bot.remove_command("help")
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Register commands (idempotent – duplicates are harmless)
    # ------------------------------------------------------------------
    bot.add_command(commands.Command(help_cmd, name="help"))
    bot.add_command(commands.Command(getid_cmd, name="getid"))
    bot.add_command(commands.Command(ping_cmd, name="ping"))

    # User config commands (authorized users only) - authorization checked inside each command
    bot.add_command(commands.Command(setmodel_cmd, name="setmodel"))
    bot.add_command(commands.Command(setsprompt_cmd, name="setsprompt"))  
    bot.add_command(commands.Command(showconfig_cmd, name="showconfig"))

    owner_check = commands.is_owner()
    bot.add_command(commands.Command(addid_cmd, name="addid", checks=[owner_check]))
    bot.add_command(commands.Command(removeid_cmd, name="removeid", checks=[owner_check]))
    bot.add_command(commands.Command(listauth_cmd, name="listauth", checks=[owner_check]))
    bot.add_command(commands.Command(memory_cmd, name="memory", checks=[owner_check]))
    bot.add_command(commands.Command(clearmemory_cmd, name="clearmemory", checks=[owner_check]))

    # ------------------------------------------------------------------
    # Register on_message listener if not already present
    # ------------------------------------------------------------------
    already = False
    try:
        existing = list(getattr(bot, "_listeners", {}).get("on_message", []))
        for l in existing:
            if getattr(l, "__qualname__", None) == on_message.__qualname__ \
               and getattr(l, "__module__", None) == on_message.__module__:
                already = True
                break
    except Exception:
        pass

    if not already:
        bot.add_listener(on_message, "on_message")
        logger.info("on_message listener registered.")
    else:
        logger.info("on_message listener already registered; not adding again.")

    logger.info("Commands registered: %s", sorted(c.name for c in bot.commands))