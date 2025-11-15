# pyats_fastmcp_server.py

import os
import re
import string
import sys
import json
import logging
import textwrap
from pyats.topology import loader
from genie.libs.parser.utils import get_parser
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import Dict, Any
import asyncio
from functools import partial
import mcp.types as types
from mcp.server.fastmcp import FastMCP
from google import genai
from google.genai import types as gtypes
from toon_format import encode as toon_encode
import tiktoken

# Initialize Gemini client
client = genai.Client()

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,  # <-- CRITICAL FIX
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("PyATSFastMCPServer")

# --- Load ENV ---
load_dotenv()
TESTBED_PATH = ("testbed.yaml")

if not TESTBED_PATH or not os.path.exists(TESTBED_PATH):
    logger.critical(
        f"❌ CRITICAL: PYATS_TESTBED_PATH missing or invalid: {TESTBED_PATH}"
    )
    sys.exit(1)

logger.info(f"✅ Using testbed file: {TESTBED_PATH}")

# --- Tokenizer initialization ---
try:
    tokenizer = tiktoken.get_encoding("o200k_base")
    logger.info("🧮 Loaded GPT o200k_base tokenizer for token savings reporting")
except Exception as e:
    logger.warning(f"⚠ GPT tokenizer unavailable: {e}")
    tokenizer = None


def count_tokens(text: str) -> int:
    """Return token count using GPT o200k_base tokenizer."""
    if tokenizer is None:
        return -1
    try:
        return len(tokenizer.encode(text))
    except Exception:
        return -1


# --- TOON Conversion w/Stats ---
def toon_with_stats(data: dict) -> str:
    json_str = json.dumps(data, indent=2)

    try:
        toon_str = toon_encode(data, keyFolding="safe", indent=2)
    except Exception as e:
        logger.error(f"TOON conversion failed: {e}", exc_info=True)
        return f"@@TOON@@\n```json\n{json_str}\n```"

    json_tokens = count_tokens(json_str)
    toon_tokens = count_tokens(toon_str)

    if json_tokens > 0 and toon_tokens > 0:
        reduction = 100 * (1 - (toon_tokens / json_tokens))
        logger.info(
            f"[TOON SAVINGS] JSON tokens: {json_tokens} | "
            f"TOON tokens: {toon_tokens} | Savings: {reduction:.1f}%"
        )

    logger.info("\n[TOON OUTPUT]\n" + toon_str + "\n")

    # THE FIX: prefix forces Gemini to treat as raw text
    return f"@@TOON@@\n```toon\n{toon_str}\n```"

# ---------------------------
# Device Connection Helpers
# ---------------------------

def _get_device(device_name: str):
    """Load testbed and return connected device."""
    try:
        testbed = loader.load(TESTBED_PATH)
        device = testbed.devices.get(device_name)

        if not device:
            raise ValueError(f"Device '{device_name}' not found in {TESTBED_PATH}")

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
        logger.error(f"Connection error on {device_name}: {e}", exc_info=True)
        raise


def _disconnect_device(device):
    """Safely disconnect."""
    if device and device.is_connected():
        try:
            logger.info(f"🔌 Disconnecting from {device.name}...")
            device.disconnect()
        except Exception as e:
            logger.warning(f"Disconnect error on {device.name}: {e}")


def clean_output(output: str) -> str:
    """Strip ANSI and non-printable chars."""
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    output = ansi_escape.sub("", output)
    return "".join(c for c in output if c in string.printable)


# ---------------------------
# pyATS Async Command Runners
# ---------------------------

async def run_show_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Run show command safely."""
    disallowed = [
        "|", "include", "exclude", "begin",
        "redirect", ">", "<", "config",
        "copy", "delete", "erase", "reload", "write"
    ]

    cmd = command.lower().strip()
    if not cmd.startswith("show"):
        return {"status": "error", "error": "Only 'show' commands allowed"}

    if any(bad in cmd.split() for bad in disallowed):
        return {"status": "error", "error": f"Disallowed modifier in '{command}'"}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_execute_show_command, device_name, command))


def _execute_show_command(device_name: str, command: str) -> Dict[str, Any]:
    device = None
    try:
        device = _get_device(device_name)

        try:
            parsed = device.parse(command)
            return {"status": "completed", "device": device_name, "output": parsed}
        except Exception as parse_exc:
            logger.warning(f"Parse failed → fallback execute: {parse_exc}")
            raw = device.execute(command)
            return {"status": "completed_raw", "device": device_name, "output": raw}

    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


async def apply_device_configuration_async(device_name: str, config_commands: str):
    if "erase" in config_commands.lower():
        return {"status": "error", "error": "Dangerous 'erase' detected"}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_execute_config, device_name, config_commands))


def _execute_config(device_name: str, config_commands: str):
    device = None
    try:
        device = _get_device(device_name)
        cleaned = textwrap.dedent(config_commands.strip())
        out = device.configure(cleaned)
        return {"status": "success", "device": device_name, "output": out}

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
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": clean_output(raw)},
        }
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
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": raw},
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


async def run_ping_command_async(device_name: str, command: str):
    if not command.lower().strip().startswith("ping"):
        return {"status": "error", "error": "Only 'ping' commands allowed"}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_execute_ping, device_name, command))


def _execute_ping(device_name: str, command: str):
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

        # Try parsers then execute raw
        try:
            parser = get_parser(command, device)
            if parser:
                out = device.parse(command)
            else:
                raise ValueError("No parser")
        except Exception:
            out = device.execute(command)

        return {"status": "completed", "device": device_name, "output": out}

    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        _disconnect_device(device)


# ---------------------------
# MCP Tools
# ---------------------------

mcp = FastMCP("pyATS Network Automation Server")


@mcp.tool()
async def pyats_run_show_command(device_name: str, command: str) -> str:
    return toon_with_stats(await run_show_command_async(device_name, command))


@mcp.tool()
async def pyats_configure_device(device_name: str, config_commands: str) -> str:
    return toon_with_stats(await apply_device_configuration_async(device_name, config_commands))


@mcp.tool()
async def pyats_show_running_config(device_name: str) -> str:
    return toon_with_stats(await execute_learn_config_async(device_name))


@mcp.tool()
async def pyats_show_logging(device_name: str) -> str:
    return toon_with_stats(await execute_learn_logging_async(device_name))


@mcp.tool()
async def pyats_ping_from_network_device(device_name: str, command: str) -> str:
    return toon_with_stats(await run_ping_command_async(device_name, command))


@mcp.tool()
async def pyats_run_linux_command(device_name: str, command: str) -> str:
    return toon_with_stats(await run_linux_command_async(device_name, command))


# ---------------------------
# File Search Tools
# ---------------------------

# @mcp.tool()
# async def upload_and_index(json_path: str) -> str:
#     """Upload a file to Gemini File Search."""
#     import os

#     if not os.path.exists(json_path):
#         raise FileNotFoundError(json_path)

#     store = client.file_search_stores.create(
#         config={"display_name": f"pyats_store"}
#     )
#     store_name = store.name

#     op = client.file_search_stores.upload_to_file_search_store(
#         file_search_store_name=store_name,
#         file=json_path,
#         config={
#             "display_name": os.path.basename(json_path),
#             "mime_type": "text/plain",
#             "chunking_config": {
#                 "white_space_config": {
#                     "max_tokens_per_chunk": 500,
#                     "max_overlap_tokens": 100,
#                 }
#             },
#         },
#     )

#     # Poll
#     op_name = op.name
#     for _ in range(60):
#         curr = client.operations.get(op_name)
#         if curr.done:
#             break
#         await asyncio.sleep(2)

#     return store_name


# @mcp.tool()
# async def analyze_router(store_name: str, question: str) -> dict:
#     """Ask Gemini 2.5 Flash with File Search grounding."""
#     resp = client.models.generate_content(
#         model="gemini-2.5-flash",
#         contents=question,
#         config=gtypes.GenerateContentConfig(
#             tools=[
#                 gtypes.Tool(
#                     file_search=gtypes.FileSearch(
#                         file_search_store_names=[store_name]
#                     )
#                 )
#             ]
#         ),
#     )

#     grounding = resp.candidates[0].grounding_metadata
#     sources = []
#     if grounding and grounding.grounding_chunks:
#         sources = [c.retrieved_context.title for c in grounding.grounding_chunks]

#     return {
#         "answer": resp.text,
#         "sources": sources,
#         "store": store_name,
#     }


# ---------------------------
# Main
# ---------------------------

if __name__ == "__main__":
    logger.info("🚀 Starting pyATS FastMCP Server with TOON + Token Savings...")
    mcp.run()
