#!/usr/bin/env python3
# coding: utf-8

import logging
import sys
import os

from discord.ext import commands
import discord

import load_config
import call_api
import functions

# logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger("discord-openai-proxy.main")

# Discord intents (message content intent must be enabled in Dev Portal)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Create bot (we keep the "!" prefix for backward compatibility)
bot = commands.Bot(command_prefix=";", intents=intents, help_command=None)

# Initialize functions module: register commands/listeners and load persisted data
functions.setup(bot, call_api, load_config)

# Optional: keep a simple on_ready here for early logging (functions.setup doesn't override)
@bot.event
async def on_ready():
    logger.info(f"Bot is ready: {bot.user} (id={bot.user.id}) pid={os.getpid()}")
    # show registered commands
    try:
        cmds = sorted([c.name for c in bot.commands])
        logger.info("Registered commands: %s", cmds)
    except Exception:
        logger.exception("Failed to list commands")

    # inspect on_message listeners
    try:
        listeners = list(getattr(bot, "_listeners", {}).get("on_message", []))
        logger.info("on_message listeners (count=%d): %s", len(listeners),
                    [f"{getattr(l,'__module__','?')}:{getattr(l,'__qualname__','?')} id={hex(id(l))}" for l in listeners])
    except Exception:
        logger.exception("Failed to inspect listeners")

if __name__ == "__main__":
    try:
        bot.run(load_config.DISCORD_TOKEN)
    except Exception:
        logger.exception("Bot exited with exception")
