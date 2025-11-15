#!/usr/bin/env python3
# pyats_fastmcp_server.py

import os
import re
import string
import sys
import json
import logging
import textwrap
from typing import Any, Dict

from pyats.topology import loader
from genie.libs.parser.utils import get_parser
from dotenv import load_dotenv
import asyncio
from functools import partial

from mcp.server.fastmcp import FastMCP
from toon_format import encode as toon_encode
import tiktoken

# ================================================================
# Logging (important: STDERR only — STDOUT reserved for MCP)
# ================================================================
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("PyATSFastMCPServer")


# ================================================================
# Testbed Path + ENV
# ================================================================
load_dotenv()

TESTBED_PATH = os.getenv("PYATS_TESTBED_PATH")

if not TESTBED_PATH or not os.path.exists(TESTBED_PATH):
    logger.critical(f"❌ CRITICAL: PYATS_TESTBED_PATH missing or invalid: {TESTBED_PATH}")
    sys.exit(1)

logger.info(f"✅ Using testbed file: {TESTBED_PATH}")


# ================================================================
# Tokenizer (for savings reporting)
# ================================================================
try:
    tokenizer = tiktoken.get_encoding("o200k_base")
    logger.info("🧮 Loaded GPT o200k_base tokenizer for token savings reporting")
except Exception as e:
    logger.warning(f"⚠ Tokenizer unavailable: {e}")
    tokenizer = None


def count_tokens(text: str) -> int:
    if tokenizer is None:
        return -1
    try:
        return len(tokenizer.encode(text))
    except Exception:
        return -1


# ================================================================
# JSON SAFE NORMALIZER — IMPORTANT FOR TOON
# ================================================================
def make_json_safe(obj: Any) -> Any:
    """Normalize pyATS/genie structures to JSON-safe primitives."""
    if isinstance(obj, dict):
        return {str(k): make_json_safe(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [make_json_safe(v) for v in obj]

    if isinstance(obj, set):
        return sorted([make_json_safe(v) for v in obj], key=lambda x: str(x))

    if hasattr(obj, "__dict__"):
        return make_json_safe(obj.__dict__)

    try:
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)


# ================================================================
# TOON CONVERSION (NO JSON FALLBACK)
# ================================================================
def toon_with_stats(data: Any) -> str:
    """
    Convert result → TOON.
    NEVER fall back to JSON.
    If TOON fails → return error block.
    """
    safe = make_json_safe(data)
    json_str = json.dumps(safe, indent=2)

    try:
        toon_str = toon_encode(safe, keyFolding="safe", indent=2)
    except Exception as e:
        logger.error("❌ TOON conversion failed", exc_info=True)
        return (
            "```error\n"
            f"TOON conversion failed:\n{e}\n"
            "Original JSON:\n"
            f"{json_str}\n"
            "```"
        )

    # Token savings
    json_tokens = count_tokens(json_str)
    toon_tokens = count_tokens(toon_str)
    if json_tokens > 0 and toon_tokens > 0:
        reduction = 100 * (1 - (toon_tokens / json_tokens))
        logger.info(
            f"[TOON SAVINGS] JSON={json_tokens} | TOON={toon_tokens} | Saved={reduction:.1f}%"
        )

    logger.info("\n[TOON OUTPUT]\n" + toon_str)

    # ALWAYS return a pure string
    return f"```toon\n{toon_str}\n```"


# ================================================================
# pyATS Device Helpers
# ================================================================
def _get_device(device_name: str):
    try:
        testbed = loader.load(TESTBED_PATH)
        device = testbed.devices.get(device_name)
        if not device:
            raise ValueError(f"Device '{device_name}' not in testbed")

        if not device.is_connected():
            logger.info(f"🔌 Connecting to {device_name}...")
            device.connect(
                connection_timeout=120,
                learn_hostname=True,
                log_stdout=False,
                mit=True,
            )
            logger.info(f"✅ Connected to {device_name}")

        return device

    except Exception as e:
        logger.error(f"Connection error for {device_name}: {e}", exc_info=True)
        raise


def _disconnect_device(device):
    if device and device.is_connected():
        try:
            logger.info(f"🔌 Disconnecting from {device.name}...")
            device.disconnect()
        except Exception as e:
            logger.warning(f"Disconnect error {device.name}: {e}")


def clean_output(output: str) -> str:
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    output = ansi_escape.sub("", output)
    return "".join(c for c in output if c in string.printable)


# ================================================================
# Async Command Runners
# ================================================================
async def run_show_command_async(device_name: str, command: str) -> Dict[str, Any]:
    disallowed = ["|", "include", "exclude", "begin", "redirect", ">", "<", "config", "copy", "delete", "erase", "reload", "write"]

    c = command.lower().strip()
    if not c.startswith("show"):
        return {"status": "error", "error": "Only 'show' commands allowed"}

    if any(x in c.split() for x in disallowed):
        return {"status": "error", "error": f"Illegal modifier in command: {command}"}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_execute_show_command, device_name, command))


def _execute_show_command(device_name: str, command: str):
    device = None
    try:
        device = _get_device(device_name)
        try:
            parsed = device.parse(command)
            return {"status": "completed", "device": device_name, "output": parsed}
        except Exception:
            raw = device.execute(command)
            return {"status": "completed_raw", "device": device_name, "output": raw}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


async def apply_device_configuration_async(device_name: str, config_commands: str):
    if "erase" in config_commands.lower():
        return {"status": "error", "error": "Unsafe 'erase' detected"}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_execute_config, device_name, config_commands))


def _execute_config(device_name: str, config_commands: str):
    device = None
    try:
        device = _get_device(device_name)
        cleaned = textwrap.dedent(config_commands.strip())
        out = device.configure(cleaned)
        return {"status": "success", "output": out}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


async def execute_learn_config_async(device_name: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_execute_learn_config, device_name))


def _execute_learn_config(device_name: str):
    device = None
    try:
        device = _get_device(device_name)
        raw = device.execute("show run brief")
        return {"status": "completed_raw", "output": {"raw_output": clean_output(raw)}}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


async def execute_learn_logging_async(device_name: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_execute_learn_logging, device_name))


def _execute_learn_logging(device_name: str):
    device = None
    try:
        device = _get_device(device_name)
        raw = device.execute("show logging last 250")
        return {"status": "completed_raw", "output": {"raw_output": raw}}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


async def run_ping_command_async(device_name: str, command: str):
    if not command.lower().startswith("ping"):
        return {"status": "error", "error": "Only 'ping' commands allowed"}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_execute_ping, device_name, command))


def _execute_ping(device_name: str, command: str):
    device = None
    try:
        device = _get_device(device_name)
        try:
            parsed = device.parse(command)
            return {"status": "completed", "output": parsed}
        except Exception:
            raw = device.execute(command)
            return {"status": "completed_raw", "output": raw}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


async def run_linux_command_async(device_name: str, command: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_execute_linux_command, device_name, command))


def _execute_linux_command(device_name: str, command: str):
    device = None
    try:
        testbed = loader.load(TESTBED_PATH)
        if device_name not in testbed.devices:
            return {"status": "error", "error": "Linux device not found"}

        device = testbed.devices[device_name]
        if not device.is_connected():
            device.connect()

        try:
            parser = get_parser(command, device)
            if parser:
                out = device.parse(command)
            else:
                raise ValueError("No parser available")
        except Exception:
            out = device.execute(command)

        return {"status": "completed", "output": out}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


# ================================================================
# MCP TOOL DEFINITIONS (ALWAYS RETURN STRING)
# ================================================================
mcp = FastMCP("pyATS Network Automation Server")


@mcp.tool()
async def pyats_run_show_command(device_name: str, command: str) -> str:
    result = await run_show_command_async(device_name, command)
    return str(toon_with_stats(result))


@mcp.tool()
async def pyats_configure_device(device_name: str, config_commands: str) -> str:
    result = await apply_device_configuration_async(device_name, config_commands)
    return str(toon_with_stats(result))


@mcp.tool()
async def pyats_show_running_config(device_name: str) -> str:
    result = await execute_learn_config_async(device_name)
    return str(toon_with_stats(result))


@mcp.tool()
async def pyats_show_logging(device_name: str) -> str:
    result = await execute_learn_logging_async(device_name)
    return str(toon_with_stats(result))


@mcp.tool()
async def pyats_ping_from_network_device(device_name: str, command: str) -> str:
    result = await run_ping_command_async(device_name, command)
    return str(toon_with_stats(result))


@mcp.tool()
async def pyats_run_linux_command(device_name: str, command: str) -> str:
    result = await run_linux_command_async(device_name, command)
    return str(toon_with_stats(result))


# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    logger.info("🚀 Starting pyATS FastMCP Server with TOON enabled…")
    mcp.run()
