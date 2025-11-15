#!/usr/bin/env python3
# pyats_fastmcp_server.py

import os
import re
import string
import sys
import json
import logging
import textwrap
import tempfile
import subprocess
from typing import Any, Dict

from pyats.topology import loader
from genie.libs.parser.utils import get_parser
from dotenv import load_dotenv
import asyncio
from functools import partial

from mcp.server.fastmcp import FastMCP
import tiktoken


# ================================================================
# LOGGING — MUST BE STDERR ONLY
# ================================================================
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("PyATSFastMCPServer")
logger.setLevel(logging.INFO)   # <── FORCE INFO LOGS TO APPEAR


# ================================================================
# ENV + TESTBED
# ================================================================
load_dotenv()

TESTBED_PATH = os.getenv("PYATS_TESTBED_PATH")
if not TESTBED_PATH or not os.path.exists(TESTBED_PATH):
    logger.critical(f"❌ CRITICAL: PYATS_TESTBED_PATH missing or invalid: {TESTBED_PATH}")
    sys.exit(1)

logger.info(f"✅ Using testbed file: {TESTBED_PATH}")


# ================================================================
# TOKENIZER (optional but great)
# ================================================================
try:
    tokenizer = tiktoken.get_encoding("o200k_base")
    logger.info("🧮 Loaded GPT o200k_base tokenizer for token savings reporting")
except Exception:
    tokenizer = None


def count_tokens(text: str) -> int:
    if tokenizer is None:
        return -1
    try:
        return len(tokenizer.encode(text))
    except Exception:
        return -1


# ================================================================
# SAFE JSON NORMALIZATION
# ================================================================
def make_json_safe(obj: Any) -> Any:
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
# TOON CONVERSION (via npx)
# ================================================================
def toon_with_stats(data: Any) -> str:

    safe = make_json_safe(data)
    json_str = json.dumps(safe, indent=2)

    # ------------------------------------------------------------------
    # Run TOON CLI via NPX
    # ------------------------------------------------------------------
    try:
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as f_json:
            f_json.write(json_str)
            f_json.flush()
            src = f_json.name
            dst = f_json.name + ".toon"

        cmd = ["npx", "@toon-format/cli", src, "-o", dst]
        # Note: Gemini will not show this log because it's INFO
        logger.info(f"[TOON] Running: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            return (
                "```error\n"
                f"TOON CLI failed:\n{result.stderr}\n\n"
                "JSON OUTPUT:\n"
                f"{json_str}\n"
                "```"
            )

        with open(dst, "r") as f:
            toon_str = f.read()

    except Exception as e:
        return (
            "```error\n"
            f"TOON subprocess error:\n{e}\n\n"
            "JSON OUTPUT:\n"
            f"{json_str}\n"
            "```"
        )

    # ------------------------------------------------------------------
    # Token savings (FORCED INTO TOOL OUTPUT)
    # ------------------------------------------------------------------
    json_tokens = count_tokens(json_str)
    toon_tokens = count_tokens(toon_str)

    if json_tokens > 0 and toon_tokens > 0:
        reduction = 100 * (1 - (toon_tokens / json_tokens))
        savings_text = (
            f"\n\n# Token Savings\n"
            f"- JSON tokens: {json_tokens}\n"
            f"- TOON tokens: {toon_tokens}\n"
            f"- Saved: {reduction:.1f}%\n"
        )
    else:
        savings_text = "\n\n# Token Savings\n(unavailable)\n"

    # ------------------------------------------------------------------
    # Return TOON + savings info bundled together
    # ------------------------------------------------------------------
    return f"```toon\n{toon_str}\n```{savings_text}"

# ================================================================
# PYATS DEVICE HELPERS
# ================================================================
def _get_device(device_name: str):
    try:
        testbed = loader.load(TESTBED_PATH)
        device = testbed.devices.get(device_name)
        if not device:
            raise ValueError(f"Device '{device_name}' not in testbed")

        if not device.is_connected():
            logger.info(f"🔌 Connecting to {device_name}…")
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
            logger.info(f"🔌 Disconnecting {device.name}…")
            device.disconnect()
        except Exception as e:
            logger.warning(f"Disconnect error {device.name}: {e}")


def clean_output(output: str) -> str:
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    output = ansi_escape.sub("", output)
    return "".join(c for c in output if c in string.printable)


# ================================================================
# COMMAND RUNNERS
# ================================================================
async def run_show_command_async(device_name: str, command: str) -> Dict[str, Any]:
    illegal = ["|", "include", "exclude", "begin", "redirect", ">", "<",
               "config", "copy", "delete", "erase", "reload", "write"]

    if not command.lower().startswith("show"):
        return {"status": "error", "error": "Only 'show' commands allowed"}

    if any(x in command.lower().split() for x in illegal):
        return {"status": "error", "error": f"Illegal modifier in '{command}'"}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, partial(_execute_show_command, device_name, command)
    )


def _execute_show_command(device_name: str, command: str):
    device = None
    try:
        device = _get_device(device_name)
        try:
            return {"status": "completed", "output": device.parse(command)}
        except Exception:
            return {"status": "completed_raw", "output": device.execute(command)}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


# ================================================================
# MCP TOOLS
# ================================================================
mcp = FastMCP("pyATS Network Automation Server")


@mcp.tool()
async def pyats_run_show_command(device_name: str, command: str) -> str:
    return toon_with_stats(await run_show_command_async(device_name, command))


# (If you want I can paste the other tools here—show logging, ping, etc.)


# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    logger.info("🚀 Starting pyATS FastMCP Server with TOON enabled…")
    mcp.run()
