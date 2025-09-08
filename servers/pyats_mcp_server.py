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
from pydantic import BaseModel, Field, ValidationError
from typing import Dict, Any, Optional
import asyncio
from functools import partial
import mcp.types as types
from mcp.server.fastmcp import FastMCP

# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("PyatsFastMCPServer")

# --- Load Environment Variables ---
load_dotenv()
TESTBED_PATH = os.getenv("PYATS_TESTBED_PATH")

if not TESTBED_PATH or not os.path.exists(TESTBED_PATH):
    logger.critical(f"❌ CRITICAL: PYATS_TESTBED_PATH environment variable not set or file not found: {TESTBED_PATH}")
    sys.exit(1)

logger.info(f"✅ Using testbed file: {TESTBED_PATH}")

# --- Pydantic Models for Input Validation ---
class DeviceCommandInput(BaseModel):
    device_name: str = Field(..., description="The name of the device in the testbed.")
    command: str = Field(..., description="The command to execute (e.g., 'show ip interface brief', 'ping 8.8.8.8').")

class ConfigInput(BaseModel):
    device_name: str = Field(..., description="The name of the device in the testbed.")
    config_commands: str = Field(..., description="Single or multi-line configuration commands.")

class DeviceOnlyInput(BaseModel):
    device_name: str = Field(..., description="The name of the device in the testbed.")

class LinuxCommandInput(BaseModel):
    device_name: str = Field(..., description="The name of the Linux device in the testbed.")
    command: str = Field(..., description="Linux command to execute (e.g., 'ifconfig', 'ls -l /home')")

# --- Core pyATS Helper Functions ---

def _get_device(device_name: str):
    """Helper to load testbed and get/connect to a device."""
    try:
        testbed = loader.load(TESTBED_PATH)
        device = testbed.devices.get(device_name)
        if not device:
            raise ValueError(f"Device '{device_name}' not found in testbed '{TESTBED_PATH}'.")

        if not device.is_connected():
            logger.info(f"Connecting to {device_name}...")
            device.connect(
                connection_timeout=120,
                learn_hostname=True,
                log_stdout=False,
                mit=True
            )
            logger.info(f"Connected to {device_name}")

        return device

    except Exception as e:
        logger.error(f"Error getting/connecting to device {device_name}: {e}", exc_info=True)
        raise

def _disconnect_device(device):
    """Helper to safely disconnect."""
    if device and device.is_connected():
        logger.info(f"Disconnecting from {device.name}...")
        try:
            device.disconnect()
            logger.info(f"Disconnected from {device.name}")
        except Exception as e:
            logger.warning(f"Error disconnecting from {device.name}: {e}")

def clean_output(output: str) -> str:
    """Clean ANSI escape sequences and non-printable characters."""
    # Remove ANSI escape sequences
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    output = ansi_escape.sub('', output)
    
    # Remove non-printable control characters
    output = ''.join(char for char in output if char in string.printable)
    
    return output

# --- Core pyATS Functions ---

async def run_show_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """Execute a show command on a device."""
    device = None
    try:
        # Validate command
        disallowed_modifiers = ['|', 'include', 'exclude', 'begin', 'redirect', '>', '<', 'config', 'copy', 'delete', 'erase', 'reload', 'write']
        command_lower = command.lower().strip()
        
        if not command_lower.startswith("show"):
            return {"status": "error", "error": f"Command '{command}' is not a 'show' command."}
        
        for part in command_lower.split():
            if part in disallowed_modifiers:
                return {"status": "error", "error": f"Command '{command}' contains disallowed term '{part}'."}

        # Execute in thread to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_show_command, device_name, command))
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
            logger.warning(f"Parsing failed for '{command}' on {device_name}: {parse_exc}. Falling back to execute.")
            raw_output = device.execute(command)
            logger.info(f"Executed command (fallback): '{command}' on {device_name}")
            return {"status": "completed_raw", "device": device_name, "output": raw_output}
            
    except Exception as e:
        logger.error(f"Error executing show command: {e}", exc_info=True)
        return {"status": "error", "error": f"Execution error: {e}"}
    finally:
        _disconnect_device(device)

async def apply_device_configuration_async(device_name: str, config_commands: str) -> Dict[str, Any]:
    """Apply configuration to a device."""
    try:
        # Safety check
        if "erase" in config_commands.lower() or "write erase" in config_commands.lower():
            logger.warning(f"Rejected potentially dangerous command on {device_name}: {config_commands}")
            return {"status": "error", "error": "Potentially dangerous command detected (erase). Operation aborted."}

        # Execute in thread to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_config, device_name, config_commands))
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
        return {"status": "success", "message": f"Configuration applied on {device_name}.", "output": output}

    except Exception as e:
        logger.error(f"Error applying configuration: {e}", exc_info=True)
        return {"status": "error", "error": f"Configuration error: {e}"}
    finally:
        _disconnect_device(device)

async def execute_learn_config_async(device_name: str) -> Dict[str, Any]:
    """Learn device configuration."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, partial(_execute_learn_config, device_name))
        return result
    except Exception as e:
        logger.error(f"Error in execute_learn_config_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning config: {e}"}

def _execute_learn_config(device_name: str) -> Dict[str, Any]:
    """Synchronous helper for learning configuration."""
    device = None
    try:
        device = _get_device(device_name)
        logger.info(f"Learning configuration from {device_name}...")
        
        device.enable()
        raw_output = device.execute("show run brief")
        cleaned_output = clean_output(raw_output)
        
        logger.info(f"Successfully learned config from {device_name}")
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": cleaned_output}
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
        result = await loop.run_in_executor(None, partial(_execute_learn_logging, device_name))
        return result
    except Exception as e:
        logger.error(f"Error in execute_learn_logging_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Error learning logs: {e}"}

def _execute_learn_logging(device_name: str) -> Dict[str, Any]:
    """Synchronous helper for learning logging."""
    device = None
    try:
        device = _get_device(device_name)
        logger.info(f"Learning logging output from {device_name}...")
        
        raw_output = device.execute("show logging last 250")
        logger.info(f"Successfully learned logs from {device_name}")
        
        return {
            "status": "completed_raw",
            "device": device_name,
            "output": {"raw_output": raw_output}
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
        result = await loop.run_in_executor(None, partial(_execute_ping, device_name, command))
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
            logger.warning(f"Parsing ping failed for '{command}' on {device_name}: {parse_exc}. Falling back to execute.")
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
        result = await loop.run_in_executor(None, partial(_execute_linux_command, device_name, command))
        return result
    except Exception as e:
        logger.error(f"Error in run_linux_command_async: {e}", exc_info=True)
        return {"status": "error", "error": f"Linux command execution error: {e}"}

def _execute_linux_command(device_name: str, command: str) -> Dict[str, Any]:
    """Synchronous helper for Linux command execution."""
    device = None
    try:
        logger.info("Loading testbed...")
        testbed = loader.load(TESTBED_PATH)
        
        if device_name not in testbed.devices:
            return {"status": "error", "error": f"Device '{device_name}' not found in testbed."}
        
        device = testbed.devices[device_name]
        
        if not device.is_connected():
            logger.info(f"Connecting to {device_name} via SSH...")
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
            logger.warning(f"No parser found for command: {command}. Using `execute` instead. Error: {e}")
            output = device.execute(command)
        
        logger.info(f"Disconnecting from {device_name}...")
        device.disconnect()
        
        return {"status": "completed", "device": device_name, "output": output}
    except Exception as e:
        logger.error(f"Error executing Linux command: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
    finally:
        if device and device.is_connected():
            try:
                device.disconnect()
            except:
                pass

# --- Initialize FastMCP ---
mcp = FastMCP("pyATS Network Automation Server")

# --- Define Tools ---

@mcp.tool()
async def pyats_run_show_command(device_name: str, command: str) -> str:
    """
    Execute a Cisco IOS/NX-OS 'show' command on a specified device.
    
    Args:
        device_name: The name of the device in the testbed
        command: The show command to execute (e.g., 'show ip interface brief')
    
    Returns:
        JSON string containing the command output
    """
    try:
        result = await run_show_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_run_show_command: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_configure_device(device_name: str, config_commands: str) -> str:
    """
    Apply configuration commands to a Cisco IOS/NX-OS device.
    
    Args:
        device_name: The name of the device in the testbed
        config_commands: Configuration commands to apply (can be multi-line)
    
    Returns:
        JSON string containing the configuration result
    """
    try:
        result = await apply_device_configuration_async(device_name, config_commands)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_configure_device: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_show_running_config(device_name: str) -> str:
    """
    Retrieve the running configuration from a Cisco IOS/NX-OS device.
    
    Args:
        device_name: The name of the device in the testbed
    
    Returns:
        JSON string containing the running configuration
    """
    try:
        result = await execute_learn_config_async(device_name)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_show_running_config: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_show_logging(device_name: str) -> str:
    """
    Retrieve recent system logs from a Cisco IOS/NX-OS device.
    
    Args:
        device_name: The name of the device in the testbed
    
    Returns:
        JSON string containing the recent logs
    """
    try:
        result = await execute_learn_logging_async(device_name)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_show_logging: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_ping_from_network_device(device_name: str, command: str) -> str:
    """
    Execute a ping command from a Cisco IOS/NX-OS device.
    
    Args:
        device_name: The name of the device in the testbed
        command: The ping command to execute (e.g., 'ping 8.8.8.8')
    
    Returns:
        JSON string containing the ping results
    """
    try:
        result = await run_ping_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_ping_from_network_device: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def pyats_run_linux_command(device_name: str, command: str) -> str:
    """
    Execute a Linux command on a specified device.
    
    Args:
        device_name: The name of the Linux device in the testbed
        command: The Linux command to execute (e.g., 'ifconfig', 'ps -ef')
    
    Returns:
        JSON string containing the command output
    """
    try:
        result = await run_linux_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_run_linux_command: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

# --- Main Function ---
if __name__ == "__main__":
    logger.info("🚀 Starting pyATS FastMCP Server...")
    mcp.run()