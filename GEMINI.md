# pyATS Network Automation — Gemini-CLI Extension

Hands-on network automation from Gemini-CLI using **pyATS/Genie** via an **MCP server**.  
Run safe show/ping/logging queries, push configs, and execute Linux commands defined in your **testbed.yaml**.

---

## Quick Install

```bash
# Replace with your repo URL or registry slug
gemini extensions install https://github.com/<you>/gemini-cli-pyats-ext.git
```

After install, the extension ships a ready-to-run testbed at:

- **macOS/Linux**: `~/.gemini/extensions/pyats/servers/testbed.yaml`
- **Windows**: `%USERPROFILE%\.gemini\extensions\pyats\servers\testbed.yaml`

**Customize it**: replace that file with your own `testbed.yaml`. No other setup required.

---

## What's Included

**MCP server**: pyATS Network Automation Server

**Tools** (invoked via `/tool`):

- `pyats_run_show_command(device_name, command)`
- `pyats_configure_device(device_name, config_commands)`
- `pyats_show_running_config(device_name)`
- `pyats_show_logging(device_name)`
- `pyats_ping_from_network_device(device_name, command)`
- `pyats_run_linux_command(device_name, command)`
- *(optional)* `pyats_testbed_info()` — see active testbed path and devices

Based on the pyATS MCP server implementation.

---

## Usage Examples

### Basic Device Information

### Basic Device Information

```
/tool pyats_run_show_command device_name="router1" command="show version"
/tool pyats_run_show_command device_name="switch1" command="show ip interface brief"
/tool pyats_run_show_command device_name="router1" command="show cdp neighbors detail"
```

### Network Connectivity Testing

```
/tool pyats_ping_from_network_device device_name="router1" command="ping 8.8.8.8"
/tool pyats_ping_from_network_device device_name="switch1" command="ping 192.168.1.1 repeat 5"
```

### Configuration Management

```
# View current configuration
/tool pyats_show_running_config device_name="router1"

# Apply configuration changes
/tool pyats_configure_device device_name="router1" config_commands="interface GigabitEthernet0/1
description Link to Core Switch
ip address 10.1.1.1 255.255.255.252
no shutdown"
```

### System Monitoring

```
# Check recent logs
/tool pyats_show_logging device_name="router1"

# Linux commands (for Linux-based network devices)
/tool pyats_run_linux_command device_name="ubuntu-server" command="top -n 1"
/tool pyats_run_linux_command device_name="ubuntu-server" command="df -h"
```

---

## Setting Up Your Testbed

The extension uses a standard pyATS `testbed.yaml` file. Here's a sample structure:

```yaml
testbed:
  name: MyNetworkTestbed

devices:
  router1:
    alias: 'Main Router'
    type: 'router'
    os: 'iosxe'
    platform: 'cat9k'
    credentials:
      default:
        username: admin
        password: cisco123
    connections:
      cli:
        protocol: ssh
        ip: 192.168.1.10
        port: 22
        
  switch1:
    alias: 'Access Switch'
    type: 'switch'
    os: 'ios'
    platform: 'cat2960'
    credentials:
      default:
        username: admin
        password: cisco123
    connections:
      cli:
        protocol: ssh
        ip: 192.168.1.20
        port: 22

  ubuntu-server:
    alias: 'Linux Server'
    type: 'linux'
    os: 'linux'
    platform: 'ubuntu'
    credentials:
      default:
        username: netadmin
        password: linux123
    connections:
      cli:
        protocol: ssh
        ip: 192.168.1.100
        port: 22
```

### Security Considerations

- Store credentials securely or use environment variables
- Consider using SSH keys instead of passwords
- The extension includes safety checks to prevent dangerous commands like `erase`
- Only `show` commands are allowed via `pyats_run_show_command`

---

## Advanced Features

### Parsed vs Raw Output

The MCP server attempts to parse command output using pyATS/Genie parsers when available:

- **Parsed output**: Structured JSON data for programmatic use
- **Raw output**: Plain text fallback when no parser is available

### Multi-line Configuration

Apply complex configurations using multi-line strings:

```
/tool pyats_configure_device device_name="router1" config_commands="
interface Loopback0
 description Management Interface
 ip address 10.0.0.1 255.255.255.255
!
router ospf 1
 network 10.0.0.0 0.0.0.255 area 0
 network 192.168.1.0 0.0.0.255 area 0
"
```

### Error Handling

The extension provides comprehensive error handling:

- Connection timeouts and retries
- Invalid command detection
- Device availability checks
- Graceful disconnection

---

## Supported Platforms

- **Cisco IOS/IOS-XE**: Full support for show commands, configuration, and ping
- **Cisco NX-OS**: Full support with NX-OS specific parsers
- **Linux**: Command execution via SSH
- **Other platforms**: Basic command execution (device-dependent)

---

## Environment Variables

Set these optional environment variables for customization:

```bash
# Path to your testbed file (overrides default location)
export PYATS_TESTBED_PATH="/path/to/your/testbed.yaml"

# Connection timeout (default: 120 seconds)
export PYATS_CONNECTION_TIMEOUT="180"

# Enable debug logging
export PYATS_DEBUG="true"
```

---

## Troubleshooting

### Common Issues

1. **Connection refused**: Check device IP addresses and SSH access
2. **Authentication failed**: Verify credentials in testbed.yaml
3. **Command not found**: Ensure the command is valid for the target platform
4. **Timeout errors**: Increase connection timeout or check network connectivity

### Debug Mode

Enable detailed logging by setting the environment variable:

```bash
export PYATS_DEBUG="true"
```

### Connectivity Test

Test basic connectivity to your devices:

```
/tool pyats_ping_from_network_device device_name="router1" command="ping 127.0.0.1"
```

---

## Contributing

Found a bug or want to add a feature? 

1. Fork the repository
2. Create a feature branch
3. Submit a pull request

---

## License

This extension is provided under the same license terms as the base pyATS MCP server implementation.

---

## Support

For issues specific to this Gemini-CLI extension:
- Check the [Issues](https://github.com/<you>/gemini-cli-pyats-ext/issues) page
- Review the pyATS documentation at [developer.cisco.com/pyats](https://developer.cisco.com/pyats/)

For general Gemini-CLI support, refer to the [Gemini-CLI documentation](https://github.com/google-gemini/gemini-cli).