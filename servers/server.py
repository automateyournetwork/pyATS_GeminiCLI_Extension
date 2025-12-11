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
    """
    Take any Python object (result dicts from pyATS helpers),
    normalize to JSON, run TOON, and append token savings stats.
    """
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
# CORE COMMAND RUNNERS
# (merged / upgraded from your second script)
# ================================================================
async def run_show_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a show command on a device with safety checks."""
    try:
        disallowed_modifiers = [
            "|", "include", "exclude", "begin", "redirect",
            ">", "<", "config", "copy", "delete", "erase", "reload", "write"
        ]
        command_lower = command.lower().strip()

        if not command_lower.startswith("show"):
            return {"status": "error", "error": f"Command '{command}' is not a 'show' command."}

        for part in command_lower.split():
            if part in disallowed_modifiers:
                return {
                    "status": "error",
                    "error": f"Command '{command}' contains disallowed term '{part}'."
                }

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, partial(_execute_show_command, device_name, command)
        )
        return result

    except Exception as e:
        logger.error(f"Error in run_show_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Execution error: {e}"}


def _execute_show_command(device_name: str, command: str) -> Dict[str, Any]:
    """Synchronous helper for show command execution."""
    device = None
    try:
        device = _get_device(device_name)

        try:
            logger.info(f"Attempting to parse command: '{command}' on {device_name}")
            parsed_output = device.parse(command)
            logger.info(f"Successfully parsed output for '{command}' on {device_name}")
            return {"status": "completed", "device": device_name, "output": parsed_output}
        except Exception as parse_exc:
            logger.warning(
                f"Parsing failed for '{command}' on {device_name}: {parse_exc}. Falling back to execute."
            )
            raw_output = device.execute(command)
            logger.info(f"Executed command (fallback): '{command}' on {device_name}")
            return {"status": "completed_raw", "device": device_name, "output": raw_output}

    except Exception as e:
        logger.error(f"Error executing show command: {e}", exc_info=True)
        return {"status": "error", "error": f"Execution error: {e}"}
    finally:
        _disconnect_device(device)


async def apply_device_configuration_async(device_name: str, config_commands: str) -> Dict[str, Any]:
    """Apply configuration to a device (with basic safety checks)."""
    try:
        if "erase" in config_commands.lower() or "write erase" in config_commands.lower():
            logger.warning(f"Rejected potentially dangerous command on {device_name}: {config_commands}")
            return {
                "status": "error",
                "error": "Potentially dangerous command detected (erase). Operation aborted."
            }

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, partial(_execute_config, device_name, config_commands)
        )
        return result

    except Exception as e:
        logger.error(f"Error in apply_device_configuration_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Configuration error: {e}"}


def _execute_config(device_name: str, config_commands: str) -> Dict[str, Any]:
    """Synchronous helper for configuration application."""
    device = None
    try:
        device = _get_device(device_name)

        cleaned_config = textwrap.dedent(config_commands.strip())
        if not cleaned_config:
            return {"status": "error", "error": "Empty configuration provided."}

        logger.info(f"Applying configuration on {device_name}:\n{cleaned_config}")
        output = device.configure(cleaned_config)
        logger.info(f"Configuration result on {device_name}: {output}")
        return {
            "status": "success",
            "message": f"Configuration applied on {device_name}.",
            "output": output,
        }

    except Exception as e:
        logger.error(f"Error applying configuration: {e}", exc_info=True)
        return {"status": "error", "error": f"Configuration error: {e}"}
    finally:
        _disconnect_device(device)


async def execute_learn_config_async(device_name: str) -> Dict[str, Any]:
    """Learn device configuration (via 'show run brief')."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, partial(_execute_learn_config, device_name)
        )
        return result
    except Exception as e:
        logger.error(f"Error in execute_learn_config_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning config: {e}"}


def _execute_learn_config(device_name: str) -> Dict[str, Any]:
    """Synchronous helper for learning configuration."""
    device = None
    try:
        device = _get_device(device_name)
        logger.info(f"Learning configuration from {device_name}…")

        device.enable()
        raw_output = device.execute("show run brief")
        cleaned_output = clean_output(raw_output)

        logger.info(f"Successfully learned config from {device_name}")
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": cleaned_output},
        }
    except Exception as e:
        logger.error(f"Error learning config: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning config: {e}"}
    finally:
        _disconnect_device(device)


async def execute_learn_logging_async(device_name: str) -> Dict[str, Any]:
    """Learn device logging."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, partial(_execute_learn_logging, device_name)
        )
        return result
    except Exception as e:
        logger.error(f"Error in execute_learn_logging_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning logs: {e}"}


def _execute_learn_logging(device_name: str) -> Dict[str, Any]:
    """Synchronous helper for learning logging."""
    device = None
    try:
        device = _get_device(device_name)
        logger.info(f"Learning logging output from {device_name}…")

        raw_output = device.execute("show logging last 250")

        logger.info(f"Successfully learned logs from {device_name}")
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": raw_output},
        }
    except Exception as e:
        logger.error(f"Error learning logs: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning logs: {e}"}
    finally:
        _disconnect_device(device)


async def run_ping_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a ping command on a device."""
    try:
        if not command.lower().strip().startswith("ping"):
            return {"status": "error", "error": f"Command '{command}' is not a 'ping' command."}

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, partial(_execute_ping, device_name, command)
        )
        return result
    except Exception as e:
        logger.error(f"Error in run_ping_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Ping execution error: {e}"}


def _execute_ping(device_name: str, command: str) -> Dict[str, Any]:
    """Synchronous helper for ping execution."""
    device = None
    try:
        device = _get_device(device_name)
        logger.info(f"Executing ping: '{command}' on {device_name}")

        try:
            parsed_output = device.parse(command)
            logger.info(f"Parsed ping output for '{command}' on {device_name}")
            return {"status": "completed", "device": device_name, "output": parsed_output}
        except Exception as parse_exc:
            logger.warning(
                f"Parsing ping failed for '{command}' on {device_name}: {parse_exc}. "
                "Falling back to execute."
            )
            raw_output = device.execute(command)
            logger.info(f"Executed ping (fallback): '{command}' on {device_name}")
            return {"status": "completed_raw", "device": device_name, "output": raw_output}
    except Exception as e:
        logger.error(f"Error executing ping: {e}", exc_info=True)
        return {"status": "error", "error": f"Ping execution error: {e}"}
    finally:
        _disconnect_device(device)


async def run_linux_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a Linux command on a device."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, partial(_execute_linux_command, device_name, command)
        )
        return result
    except Exception as e:
        logger.error(f"Error in run_linux_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Linux command execution error: {e}"}


def _execute_linux_command(device_name: str, command: str) -> Dict[str, Any]:
    """Synchronous helper for Linux command execution."""
    device = None
    try:
        logger.info("Loading testbed…")
        testbed = loader.load(TESTBED_PATH)

        if device_name not in testbed.devices:
            return {"status": "error", "error": f"Device '{device_name}' not found in testbed."}

        device = testbed.devices[device_name]

        if not device.is_connected():
            logger.info(f"Connecting to {device_name} via SSH…")
            device.connect()

        if ">" in command or "|" in command:
            logger.info(f"Detected redirection or pipe in command: {command}")
            command = f'sh -c "{command}"'

        try:
            parser = get_parser(command, device)
            if parser:
                logger.info(f"Parsing output for command: {command}")
                output = device.parse(command)
            else:
                raise ValueError("No parser available")
        except Exception as e:
            logger.warning(
                f"No parser found for command: {command}. Using `execute` instead. Error: {e}"
            )
            output = device.execute(command)

        logger.info(f"Disconnecting from {device_name}…")
        device.disconnect()

        return {"status": "completed", "device": device_name, "output": output}
    except Exception as e:
        logger.error(f"Error executing Linux command: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
    finally:
        if device and device.is_connected():
            try:
                device.disconnect()
            except Exception:
                pass


# ================================================================
# MCP TOOLS (now all TOON-ified)
# ================================================================
mcp = FastMCP("pyATS Network Automation Server")


@mcp.tool()
async def pyats_run_show_command(device_name: str, command: str) -> str:
    """
    Execute a Cisco IOS/NX-OS 'show' command on a specified device.
    Returns TOON + token savings.
    """
    result = await run_show_command_async(device_name, command)
    return toon_with_stats(result)


@mcp.tool()
async def pyats_configure_device(device_name: str, config_commands: str) -> str:
    """
    Apply configuration commands to a Cisco IOS/NX-OS device.
    Returns TOON + token savings.
    """
    result = await apply_device_configuration_async(device_name, config_commands)
    return toon_with_stats(result)


@mcp.tool()
async def pyats_show_running_config(device_name: str) -> str:
    """
    Retrieve the running configuration from a Cisco IOS/NX-OS device.
    Returns TOON + token savings.
    """
    result = await execute_learn_config_async(device_name)
    return toon_with_stats(result)


@mcp.tool()
async def pyats_show_logging(device_name: str) -> str:
    """
    Retrieve recent system logs from a Cisco IOS/NX-OS device.
    Returns TOON + token savings.
    """
    result = await execute_learn_logging_async(device_name)
    return toon_with_stats(result)


@mcp.tool()
async def pyats_ping_from_network_device(device_name: str, command: str) -> str:
    """
    Execute a ping command from a Cisco IOS/NX-OS device.
    Returns TOON + token savings.
    """
    result = await run_ping_command_async(device_name, command)
    return toon_with_stats(result)


@mcp.tool()
async def pyats_run_linux_command(device_name: str, command: str) -> str:
    """
    Execute a Linux command on a specified device.
    Returns TOON + token savings.
    """
    result = await run_linux_command_async(device_name, command)
    return toon_with_stats(result)


# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    logger.info("🚀 Starting pyATS FastMCP Server with TOON enabled…")
    mcp.run()
