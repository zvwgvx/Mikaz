#!/usr/bin/env python3
# coding: utf-8
# ────────────────────────────────────────────────────────────────────────
# Bot helper / command registry
# Uses MemoryStore for per‑user conversation history
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

logger = logging.getLogger("discord-openai-proxy.functions")

# ---------------------------- module‑level state -----------------------------
_bot: Optional[commands.Bot] = None
_call_api = None
_config = None
_SYSTEM_PROMPT: Optional[Dict[str, str]] = None

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
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            arr = data.get("authorized", [])
            return set(int(x) for x in arr)
        except Exception:
            logger.exception("Failed to load authorized.json, returning empty set.")
    return set()


def save_authorized_to_path(path: Path, s: Set[int]) -> None:
    try:
        path.write_text(json.dumps({"authorized": sorted(list(s))}, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save authorized.json")

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

    _authorized_users.add(uid)
    save_authorized_to_path(_config.AUTHORIZED_STORE, _authorized_users)
    await ctx.send(f"Added ID {uid} to authorized.json.", allowed_mentions=discord.AllowedMentions.none())


async def removeid_cmd(ctx: commands.Context, id_or_mention: str):
    global _authorized_users
    uid = _extract_user_id_from_str(id_or_mention)
    if uid is None:
        await ctx.send("Invalid parameter. Provide a user ID or a mention.", allowed_mentions=discord.AllowedMentions.none())
        return

    if uid not in _authorized_users:
        await ctx.send(f"ID {uid} is not in the authorized list.", allowed_mentions=discord.AllowedMentions.none())
        return

    _authorized_users.remove(uid)
    save_authorized_to_path(_config.AUTHORIZED_STORE, _authorized_users)
    await ctx.send(f"Removed ID {uid} from authorized.json.", allowed_mentions=discord.AllowedMentions.none())

async def listauth_cmd(ctx: commands.Context):
    if not _authorized_users:
        await ctx.send("Authorized list is empty.", allowed_mentions=discord.AllowedMentions.none())
        return

    body = "\n".join(str(x) for x in sorted(_authorized_users))
    if len(body) > 1900:
        fp = _config.AUTHORIZED_STORE if _config.AUTHORIZED_STORE.exists() else None
        if fp:
            await ctx.send("List too long, sending authorized.json file.", allowed_mentions=discord.AllowedMentions.none(), file=discord.File(fp))
        else:
            await ctx.send("List too long, authorized.json not found.", allowed_mentions=discord.AllowedMentions.none())
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
# Message formatting helpers
# ------------------------------------------------------------------
def convert_latex_to_discord(text: str) -> str:
    """
    Chuyển đổi các ký hiệu LaTeX thành text/Unicode phù hợp với Discord
    IMPROVED: Xử lý tốt hơn các công thức phức tạp và formatting
    """
    latex_conversions = {
        # Math operators
        r'\oplus': '⊕',  # XOR symbol
        r'\neq': '≠',    # Not equal
        r'\leq': '≤',    # Less than or equal
        r'\geq': '≥',    # Greater than or equal
        r'\in': '∈',     # Element of
        r'\notin': '∉',  # Not element of
        r'\subset': '⊂', # Subset
        r'\supset': '⊃', # Superset
        r'\subseteq': '⊆', # Subset or equal
        r'\supseteq': '⊇', # Superset or equal
        r'\cap': '∩',    # Intersection
        r'\cup': '∪',    # Union
        r'\emptyset': '∅', # Empty set
        r'\varnothing': '∅', # Empty set (variant)
        r'\infty': '∞',  # Infinity
        r'\pm': '±',     # Plus minus
        r'\mp': '∓',     # Minus plus
        r'\times': '×',  # Times
        r'\div': '÷',    # Division
        r'\cdot': '·',   # Center dot
        r'\bullet': '•', # Bullet
        r'\circ': '∘',   # Circle operator
        r'\ast': '∗',    # Asterisk operator
        r'\star': '⋆',   # Star operator
        r'\bigstar': '★', # Big star
        
        # Logic
        r'\land': '∧',   # Logical AND
        r'\lor': '∨',    # Logical OR
        r'\neg': '¬',    # Logical NOT
        r'\lnot': '¬',   # Logical NOT (alternative)
        r'\implies': '⇒', # Implies
        r'\impliedby': '⇐', # Implied by
        r'\iff': '⇔',    # If and only if
        r'\forall': '∀', # For all
        r'\exists': '∃', # There exists
        r'\nexists': '∄', # Does not exist
        
        # Greek letters (comprehensive)
        r'\alpha': 'α', r'\Alpha': 'Α',
        r'\beta': 'β', r'\Beta': 'Β',
        r'\gamma': 'γ', r'\Gamma': 'Γ',
        r'\delta': 'δ', r'\Delta': 'Δ',
        r'\epsilon': 'ε', r'\varepsilon': 'ε', r'\Epsilon': 'Ε',
        r'\zeta': 'ζ', r'\Zeta': 'Ζ',
        r'\eta': 'η', r'\Eta': 'Η',
        r'\theta': 'θ', r'\vartheta': 'θ', r'\Theta': 'Θ',
        r'\iota': 'ι', r'\Iota': 'Ι',
        r'\kappa': 'κ', r'\Kappa': 'Κ',
        r'\lambda': 'λ', r'\Lambda': 'Λ',
        r'\mu': 'μ', r'\Mu': 'Μ',
        r'\nu': 'ν', r'\Nu': 'Ν',
        r'\xi': 'ξ', r'\Xi': 'Ξ',
        r'\omicron': 'ο', r'\Omicron': 'Ο',
        r'\pi': 'π', r'\Pi': 'Π',
        r'\rho': 'ρ', r'\varrho': 'ρ', r'\Rho': 'Ρ',
        r'\sigma': 'σ', r'\varsigma': 'ς', r'\Sigma': 'Σ',
        r'\tau': 'τ', r'\Tau': 'Τ',
        r'\upsilon': 'υ', r'\Upsilon': 'Υ',
        r'\phi': 'φ', r'\varphi': 'φ', r'\Phi': 'Φ',
        r'\chi': 'χ', r'\Chi': 'Χ',
        r'\psi': 'ψ', r'\Psi': 'Ψ',
        r'\omega': 'ω', r'\Omega': 'Ω',
        
        # Arrows
        r'\rightarrow': '→', r'\to': '→',
        r'\leftarrow': '←', r'\gets': '←',
        r'\leftrightarrow': '↔',
        r'\uparrow': '↑',
        r'\downarrow': '↓',
        r'\updownarrow': '↕',
        r'\Rightarrow': '⇒',
        r'\Leftarrow': '⇐',
        r'\Leftrightarrow': '⇔',
        r'\Uparrow': '⇑',
        r'\Downarrow': '⇓',
        r'\Updownarrow': '⇕',
        r'\nearrow': '↗',
        r'\searrow': '↘',
        r'\swarrow': '↙',
        r'\nwarrow': '↖',
        
        # Set theory and relations
        r'\approx': '≈',
        r'\sim': '∼',
        r'\cong': '≅',
        r'\equiv': '≡',
        r'\prec': '≺',
        r'\succ': '≻',
        r'\preceq': '⪯',
        r'\succeq': '⪰',
        r'\parallel': '∥',
        r'\perp': '⊥',
        
        # Calculus
        r'\int': '∫',
        r'\iint': '∬',
        r'\iiint': '∭',
        r'\oint': '∮',
        r'\partial': '∂',
        r'\nabla': '∇',
        r'\sum': 'Σ',
        r'\prod': 'Π',
        r'\coprod': '∐',
        
        # Number sets
        r'\mathbb{N}': 'ℕ',
        r'\mathbb{Z}': 'ℤ',
        r'\mathbb{Q}': 'ℚ',
        r'\mathbb{R}': 'ℝ',
        r'\mathbb{C}': 'ℂ',
        r'\mathbb{P}': 'ℙ',
        
        # Brackets and groupings
        r'\{': '{', r'\}': '}',
        r'\langle': '⟨', r'\rangle': '⟩',
        r'\lfloor': '⌊', r'\rfloor': '⌊',
        r'\lceil': '⌈', r'\rceil': '⌉',
        r'\lvert': '|', r'\rvert': '|',
        r'\lVert': '‖', r'\rVert': '‖',
        
        # Misc symbols
        r'\dots': '…', r'\ldots': '…', r'\cdots': '⋯',
        r'\vdots': '⋮', r'\ddots': '⋱',
        r'\hbar': 'ℏ',
        r'\ell': 'ℓ',
        r'\Re': 'ℜ', r'\Im': 'ℑ',
        r'\wp': '℘',
        r'\deg': '°',
        r'\angle': '∠',
        r'\triangle': '△',
        r'\square': '□',
        r'\diamond': '◊',
        r'\clubsuit': '♣',
        r'\diamondsuit': '♢',
        r'\heartsuit': '♡',
        r'\spadesuit': '♠',
    }
    
    # Chia text thành từng dòng để xử lý riêng biệt
    lines = text.split('\n')
    processed_lines = []
    
    for line in lines:
        # Bảo toàn leading whitespace
        leading_whitespace = ''
        stripped_line = line
        
        match = re.match(r'^(\s*)', line)
        if match:
            leading_whitespace = match.group(1)
            stripped_line = line[len(leading_whitespace):]
        
        # === PREPROCESSING: Xử lý các construct phức tạp trước ===
        
        # 1. Xử lý matrices (bmatrix, pmatrix, matrix, etc.)
        # Convert \begin{bmatrix}...\end{bmatrix} to [...]
        stripped_line = re.sub(r'\\begin\{[bp]?matrix\}(.*?)\\end\{[bp]?matrix\}', 
                              lambda m: '[' + m.group(1).replace('\\\\', '; ').replace('&', ', ') + ']', 
                              stripped_line, flags=re.DOTALL)
        
        # 2. Xử lý các function names phổ biến
        function_names = [
            'log', 'ln', 'exp', 'sin', 'cos', 'tan', 'cot', 'sec', 'csc',
            'arcsin', 'arccos', 'arctan', 'sinh', 'cosh', 'tanh',
            'min', 'max', 'sup', 'inf', 'lim', 'det', 'tr', 'rank',
            'dim', 'ker', 'span', 'gcd', 'lcm'
        ]
        for func in function_names:
            # Replace \func with func (remove backslash)
            stripped_line = re.sub(rf'\\{func}\b', func, stripped_line)
        
        # 3. Xử lý fractions \frac{a}{b} -> (a)/(b)
        # Improved to handle nested braces better
        def replace_frac(match):
            num = match.group(1)
            den = match.group(2)
            # If numerator or denominator is single character/simple, don't need parentheses
            if re.match(r'^[a-zA-Z0-9]$', num.strip()) and re.match(r'^[a-zA-Z0-9]$', den.strip()):
                return f'{num}/{den}'
            return f'({num})/({den})'
        
        # Handle nested fractions iteratively
        while r'\frac{' in stripped_line:
            stripped_line = re.sub(r'\\frac\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', 
                                 replace_frac, stripped_line)
        
        # 4. Xử lý powers và limits
        # \sum_{i=1}^{n} -> Σ(i=1 to n)
        stripped_line = re.sub(r'\\sum_\{([^}]+)\}\^\{([^}]+)\}', r'Σ(\1 to \2)', stripped_line)
        stripped_line = re.sub(r'\\prod_\{([^}]+)\}\^\{([^}]+)\}', r'Π(\1 to \2)', stripped_line)
        stripped_line = re.sub(r'\\int_\{([^}]+)\}\^\{([^}]+)\}', r'∫[\1 to \2]', stripped_line)
        
        # 5. Xử lý limits
        stripped_line = re.sub(r'\\lim_\{([^}]+)\\to([^}]+)\}', r'lim(\1→\2)', stripped_line)
        
        # === MAIN CONVERSIONS ===
        
        # Apply LaTeX symbol conversions
        for latex, unicode_char in latex_conversions.items():
            # Use word boundaries to avoid partial matches
            pattern = latex.replace('\\', r'\\') + r'\b'
            stripped_line = re.sub(pattern, unicode_char, stripped_line)
        
        # === SUBSCRIPTS AND SUPERSCRIPTS ===
        
        # Enhanced subscript handling
        subscript_map = {
            '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄', 
            '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉',
            'a': 'ₐ', 'e': 'ₑ', 'i': 'ᵢ', 'j': 'ⱼ', 'k': 'ₖ', 
            'l': 'ₗ', 'm': 'ₘ', 'n': 'ₙ', 'o': 'ₒ', 'p': 'ₚ',
            'r': 'ᵣ', 's': 'ₛ', 't': 'ₜ', 'u': 'ᵤ', 'v': 'ᵥ',
            'x': 'ₓ', '+': '₊', '-': '₋', '=': '₌', '(': '₍', ')': '₎'
        }
        
        # Handle complex subscripts _{...}
        def replace_subscript_complex(match):
            content = match.group(2)
            result = ''
            for char in content:
                result += subscript_map.get(char, char)
            return match.group(1) + result
        
        stripped_line = re.sub(r'([a-zA-Z0-9])_\{([^}]+)\}', replace_subscript_complex, stripped_line)
        
        # Handle simple subscripts _x
        for char, sub in subscript_map.items():
            stripped_line = re.sub(rf'([a-zA-Z0-9])_{re.escape(char)}', rf'\1{sub}', stripped_line)
        
        # Enhanced superscript handling
        superscript_map = {
            '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
            '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
            '+': '⁺', '-': '⁻', '=': '⁼', '(': '⁽', ')': '⁾',
            'n': 'ⁿ', 'i': 'ⁱ', 'j': 'ʲ', 'k': 'ᵏ', 'T': 'ᵀ'
        }
        
        # Handle complex superscripts ^{...}
        def replace_superscript_complex(match):
            content = match.group(2)
            result = ''
            for char in content:
                result += superscript_map.get(char, char)
            return match.group(1) + result
        
        stripped_line = re.sub(r'([a-zA-Z0-9\)])(\^\{[^}]+\})', replace_superscript_complex, stripped_line)
        
        # Handle simple superscripts ^x
        for char, sup in superscript_map.items():
            stripped_line = re.sub(rf'(\w)\^{re.escape(char)}', rf'\1{sup}', stripped_line)
        
        # === CLEANUP ===
        
        # Remove remaining LaTeX commands that we couldn't convert
        stripped_line = re.sub(r'\\[a-zA-Z]+\*?', '', stripped_line)
        
        # Clean up extra braces that might be left
        stripped_line = re.sub(r'\{([^{}]*)\}', r'\1', stripped_line)
        
        # Clean up multiple spaces in content (but preserve indentation)
        stripped_line = re.sub(r' {2,}', ' ', stripped_line).rstrip()
        
        # Fix spacing around mathematical operators
        stripped_line = re.sub(r'([=+\-*/])', r' \1 ', stripped_line)  # Add spaces around operators
        stripped_line = re.sub(r' {2,}', ' ', stripped_line)  # Clean up double spaces again
        
        # Ghép lại với leading whitespace
        processed_line = leading_whitespace + stripped_line
        processed_lines.append(processed_line)
    
    # Ghép lại các dòng
    result = '\n'.join(processed_lines)
    
    return result


def split_message_smart(text: str, max_length: int = 2000) -> list[str]:
    """
    Chia tin nhắn một cách thông minh, tránh phá vỡ code blocks
    IMPROVED: Bảo toàn indentation khi chia tin nhắn
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current_chunk = ""
    in_code_block = False
    code_block_lang = ""
    
    lines = text.split('\n')
    
    for line in lines:
        # Kiểm tra xem có phải là code block delimiter không
        code_match = re.match(r'^```(\w*)', line.strip())
        if code_match:
            if not in_code_block:
                # Bắt đầu code block
                in_code_block = True
                code_block_lang = code_match.group(1)
            else:
                # Kết thúc code block
                in_code_block = False
                code_block_lang = ""
        
        # Thêm line vào chunk hiện tại
        test_chunk = current_chunk + ('\n' if current_chunk else '') + line
        
        if len(test_chunk) > max_length:
            if current_chunk:
                # Nếu đang trong code block, đóng nó trước khi chia
                if in_code_block:
                    current_chunk += '\n```'
                    chunks.append(current_chunk)
                    # Bắt đầu chunk mới với code block
                    current_chunk = f'```{code_block_lang}\n{line}'
                else:
                    chunks.append(current_chunk)
                    current_chunk = line
            else:
                # Line quá dài, cần chia nhỏ hơn
                if len(line) > max_length:
                    # Chia line dài thành nhiều phần - BẢO TOÀN indentation
                    original_line = line
                    leading_whitespace = ''
                    
                    # Lưu leading whitespace
                    match = re.match(r'^(\s*)', original_line)
                    if match:
                        leading_whitespace = match.group(1)
                        line_content = original_line[len(leading_whitespace):]
                    else:
                        line_content = original_line
                    
                    # Chia content thành các phần
                    while len(line_content) > max_length - len(leading_whitespace):
                        # Tính toán space available cho content
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
    
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks


async def send_long_message(channel, content: str, max_msg_length: int = 2000):
    """
    Gửi tin nhắn dài, tự động chia nhỏ nếu cần
    """
    if len(content) <= max_msg_length:
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        return
    
    chunks = split_message_smart(content, max_msg_length)
    
    for i, chunk in enumerate(chunks):
        if i > 0:  # Thêm delay giữa các tin nhắn
            await asyncio.sleep(0.3)
        await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())

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
    # Store the user's message in memory first (before API call)
    # ------------------------------------------------------------------
    if _memory_store:
        _memory_store.add_message(message.author.id, {"role": "user", "content": final_user_text})

    # Build payload for OpenAI
    user_memory = _memory_store.get_user_messages(message.author.id) if _memory_store else []
    payload_messages = [_SYSTEM_PROMPT] + user_memory + [{"role": "user", "content": final_user_text}]

    # ------------------------------------------------------------------
    # Call OpenAI
    # ------------------------------------------------------------------
    try:
        async with message.channel.typing():
            loop = asyncio.get_running_loop()
            ok, resp = await loop.run_in_executor(None, _call_api.call_openai_proxy, payload_messages)
    except Exception as e:
        logger.exception("Error calling openai proxy async")
        await message.channel.send(f"Internal error: {e}", allowed_mentions=discord.AllowedMentions.none())
        return

    if not ok:
        await message.channel.send(f"[OpenAI PROXY ERROR] {resp}", allowed_mentions=discord.AllowedMentions.none())
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
    # Send reply to Discord
    # ------------------------------------------------------------------
    try:
        await send_long_message(message.channel, reply, _config.MAX_MSG)
    except Exception:
        logger.exception("Error sending reply to Discord")

# ------------------------------------------------------------------
# Setup – register commands, listeners, load data
# ------------------------------------------------------------------
def setup(bot: commands.Bot, call_api_module, config_module):
    global _bot, _call_api, _config, _SYSTEM_PROMPT, _authorized_users, _memory_store

    _bot = bot
    _call_api = call_api_module
    _config = config_module
    _SYSTEM_PROMPT = config_module.load_system_prompt()

    # Load authorized users
    _authorized_users = load_authorized_from_path(_config.AUTHORIZED_STORE)
    logger.info("Functions module initialized. Authorized users: %s", sorted(_authorized_users))

    # Initialize memory store
    _memory_store = MemoryStore()
    logger.info("Memory store: %d users cached", len(_memory_store._cache))

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