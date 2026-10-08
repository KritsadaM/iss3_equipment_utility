"""
Interactive CLI transport for equipment that is controlled through a
command shell rather than an HTTP API (e.g. the APC Network Management
Card CLI over SSH).

The transport only moves text: send a command line, read until the shell
prompt comes back, return what the device printed in between. Parsing the
device's reply is left to the driver.

paramiko is imported lazily so that importing equipment_drivers does not
require it; only drivers that actually open an SSH session need it.
"""
import logging
import re
import socket
import time

from equipment_drivers.exceptions import EquipmentConnectionError

logger = logging.getLogger(__name__)
# paramiko logs every handshake step at INFO; keep only its warnings and errors.
logging.getLogger("paramiko").setLevel(logging.WARNING)

# APC NMC prompts look like "apc>" (or "<user>@apc>" on some firmware).
DEFAULT_PROMPT_PATTERN = r"[\w.@-]*>\s*$"


class SshCliTransport:
    def __init__(self, prompt_pattern: str = DEFAULT_PROMPT_PATTERN):
        self._prompt_re = re.compile(prompt_pattern)
        self._client = None
        self._chan = None
        self.timeout = 10.0
        self.banner = ""

    def open(self, host: str, port: int, username: str, password: str, timeout: float = 10.0) -> None:
        try:
            import paramiko
        except ImportError:
            raise EquipmentConnectionError("SSH CLI support requires the 'paramiko' package (pip install paramiko)")

        self.timeout = timeout
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        # Lab equipment is commonly reached by IP with no known_hosts entry;
        # accept unknown keys but log them rather than failing outright.
        self._client.set_missing_host_key_policy(_log_unknown_host_key_policy(paramiko))
        try:
            self._client.connect(host, port=port, username=username, password=password, timeout=timeout,
                                 look_for_keys=False, allow_agent=False)
            self._chan = self._client.invoke_shell(width=200, height=1000)
            self._chan.settimeout(timeout)
            # Everything up to the first prompt is the login banner.
            lines = self._read_until_prompt().split("\n")
            if lines and self._prompt_re.search(lines[-1]):
                lines = lines[:-1]
            self.banner = "\n".join(lines).strip("\n")
        except Exception as e:
            self.close()
            raise EquipmentConnectionError(f"SSH session to {host}:{port} failed: {e}")

    def send(self, command: str) -> str:
        """Send one command line and return the device's reply, without the
        echoed command or the trailing prompt."""
        if self._chan is None:
            raise EquipmentConnectionError("SSH session is not open")
        logger.debug(f"CLI >> {command}")
        self._chan.send(command + "\r")
        output = self._read_until_prompt()

        lines = output.split("\n")
        if lines and lines[0].strip() == command.strip():
            lines = lines[1:]
        if lines and self._prompt_re.search(lines[-1]):
            lines = lines[:-1]
        return "\n".join(lines).strip("\n")

    def close(self) -> None:
        if self._chan is not None:
            try:
                self._chan.close()
            except Exception:
                pass
            self._chan = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def _read_until_prompt(self) -> str:
        buf = ""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                chunk = self._chan.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            normalized = buf.replace("\r\n", "\n").replace("\r", "\n")
            if self._prompt_re.search(normalized):
                return normalized
        raise EquipmentConnectionError(f"Timed out waiting for CLI prompt; received: {buf[-200:]!r}")



def _log_unknown_host_key_policy(paramiko):
    class Policy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            logger.warning(f"Unknown SSH host key for {hostname} ({key.get_name()} {key.get_fingerprint().hex()})")
    return Policy()
