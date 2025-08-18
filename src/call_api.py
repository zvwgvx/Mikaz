# call_api.py
import logging
import os
from openai import OpenAI
import load_config  # changed from relative import to absolute import

logger = logging.getLogger("discord-openai-proxy.call_api")

# Initialize OpenAI client (proxy support)
try:
    openai_client = OpenAI(api_key=load_config.OPENAI_API_KEY, base_url=load_config.OPENAI_API_BASE)
except TypeError:
    # fallback constructor if SDK signature differs
    openai_client = OpenAI(api_key=load_config.OPENAI_API_KEY)


def call_openai_proxy(messages, model=None):
    """Call OpenAI chat completions via proxy client.
    Args:
        messages: List of message dictionaries for the conversation
        model: Model to use (optional, defaults to config model)
    Returns (ok: bool, content_or_error: str)
    Keeps behavior same as original code.
    """
    # Use provided model or fall back to config default
    selected_model = model or load_config.OPENAI_MODEL
    
    try:
        resp = openai_client.chat.completions.create(
            model=selected_model,
            messages=messages,
        )
    except Exception as e:
        logger.exception("Error calling OpenAI proxy")
        return False, f"OpenAI proxy connection error: {e}"

    try:
        try:
            choice0 = resp.choices[0]
        except Exception:
            choice0 = None

        if choice0 is None:
            return True, str(resp)

        content = None
        if hasattr(choice0, "message"):
            m = choice0.message
            if isinstance(m, dict):
                content = m.get("content")
            else:
                content = getattr(m, "content", None)
        else:
            if isinstance(choice0, dict):
                content = choice0.get("message", {}).get("content") if choice0.get("message") else choice0.get("text")
            else:
                content = getattr(choice0, "text", None)

        if content is None:
            return True, str(choice0)

        return True, content

    except Exception:
        logger.exception("Failed to parse response from OpenAI proxy")
        try:
            return True, str(resp)
        except Exception:
            return False, "Unable to read response from OpenAI proxy"