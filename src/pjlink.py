"""Minimal asyncio PJLink Class 1/2 client.

Opens a fresh TCP connection per transaction, because many projectors
drop idle PJLink connections after a few seconds. Transactions are
serialized with a lock so we never open parallel connections.
"""

import asyncio
import hashlib

DEFAULT_PORT = 4352
DEFAULT_TIMEOUT = 8.0

ERROR_MESSAGES = {
    "ERR1": "Command not supported by this projector",
    "ERR2": "Invalid parameter",
    "ERR3": "Command unavailable in current state",
    "ERR4": "Projector/display failure",
    "ERRA": "PJLink authentication failed (wrong password?)",
}


class PJLinkError(Exception):
    """PJLink protocol level error (ERR1..ERR4, ERRA)."""

    def __init__(self, code: str, command: str | None = None):
        self.code = code
        self.command = command
        message = ERROR_MESSAGES.get(code, code)
        if command:
            message = f"{message} ({command})"
        super().__init__(message)


def input_name(code: str) -> str:
    """Map a PJLink input code to a human readable default name."""
    kind, index = code[:1], code[1:]
    return {
        "1": f"RGB {index}",
        "2": f"Video {index}",
        "3": f"HDMI/Digital {index}",
        "4": f"Storage {index}",
        "5": f"Network {index}",
        "6": f"Internal {index}",
    }.get(kind, f"Input {code}")


def parse_error_status(payload: str) -> dict[str, list[str]]:
    """Parse an ERST response ("000000") into warnings/errors lists."""
    names = ["fan", "lamp", "temperature", "cover", "filter", "other"]
    result: dict[str, list[str]] = {"warnings": [], "errors": []}
    for i, name in enumerate(names):
        if i >= len(payload):
            break
        if payload[i] == "1":
            result["warnings"].append(name)
        elif payload[i] == "2":
            result["errors"].append(name)
    return result


def parse_lamp(payload: str) -> list[dict]:
    """Parse a LAMP response ("1234 1" or "1234 1 5678 0")."""
    parts = payload.split()
    lamps = []
    for i in range(0, len(parts) - 1, 2):
        try:
            lamps.append({"hours": int(parts[i]), "on": parts[i + 1] == "1"})
        except ValueError:
            continue
    return lamps


class PJLinkClient:
    """PJLink client for a single projector."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, password: str = "",
                 timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.password = password or ""
        self.timeout = timeout
        self._lock = asyncio.Lock()

    async def command(self, cmd: str, param: str, pj_class: int = 1) -> str:
        """Run a single command, return the response payload."""
        result = (await self.transaction([(cmd, param, pj_class)]))[0]
        if isinstance(result, PJLinkError):
            raise result
        return result

    async def transaction(self, commands: list[tuple]) -> list:
        """Run commands over one connection.

        Each command is (cmd, param) or (cmd, param, pjlink_class).
        Returns a list of payload strings; per-command protocol errors
        (ERR1..ERR4) are returned as PJLinkError instances instead of raised.
        Connection and authentication errors are raised.
        """
        async with self._lock:
            # Some projectors (e.g. Acer) refuse rapid successive TCP
            # connections, so retry connection-level failures with a backoff.
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    return await asyncio.wait_for(
                        self._transaction(commands), self.timeout * (len(commands) + 1)
                    )
                except PJLinkError:
                    raise
                except (OSError, ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError) as err:
                    last_err = err
                    await asyncio.sleep(0.4 * (attempt + 1))
            raise ConnectionError(f"PJLink connection failed: {last_err}") from last_err

    async def _transaction(self, commands: list[tuple]) -> list:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )
        try:
            # NUL stripping: some projectors (e.g. Acer) append \x00 after responses
            greeting = (await asyncio.wait_for(reader.readuntil(b"\r"), self.timeout)).replace(b"\x00", b"").decode().strip()

            auth_prefix = ""
            if greeting.upper().startswith("PJLINK 1 "):
                seed = greeting.split(" ")[2]
                if not self.password:
                    raise PJLinkError("ERRA")
                auth_prefix = hashlib.md5((seed + self.password).encode()).hexdigest()
            elif "ERRA" in greeting.upper():
                raise PJLinkError("ERRA")
            elif not greeting.upper().startswith("PJLINK 0"):
                raise ConnectionError(f"Unexpected PJLink greeting: {greeting}")

            results = []
            for i, command in enumerate(commands):
                cmd, param = command[0], command[1]
                pj_class = command[2] if len(command) > 2 else 1
                prefix = auth_prefix if i == 0 else ""
                writer.write(f"{prefix}%{pj_class}{cmd} {param}\r".encode())
                await writer.drain()

                line = (await asyncio.wait_for(reader.readuntil(b"\r"), self.timeout)).replace(b"\x00", b"").decode().strip()
                if "ERRA" in line.upper() and line.upper().startswith("PJLINK"):
                    raise PJLinkError("ERRA")
                if "=" not in line:
                    raise ConnectionError(f"Unexpected PJLink response: {line}")
                payload = line.split("=", 1)[1].strip()
                if payload.upper() in ("ERR1", "ERR2", "ERR3", "ERR4"):
                    results.append(PJLinkError(payload.upper(), cmd))
                elif payload.upper() == "ERRA":
                    raise PJLinkError("ERRA")
                else:
                    results.append(payload)
            return results
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pylint: disable=broad-except
                pass

    async def get_power(self) -> str:
        """Return power state: off / on / cooling / warming."""
        payload = await self.command("POWR", "?")
        return {"0": "off", "1": "on", "2": "cooling", "3": "warming"}.get(payload, "off")

    async def get_input_list(self) -> list[dict]:
        """Return [{"id": code, "name": name}] using INST and (class 2) INNM."""
        clss = 1
        try:
            clss = int(await self.command("CLSS", "?"))
        except (PJLinkError, ValueError):
            pass

        inst = await self.command("INST", "?")
        codes = inst.split()

        # Query all input names over a single connection - some projectors
        # refuse rapid successive connections
        names: list = []
        if clss >= 2 and codes:
            try:
                names = await self.transaction([("INNM", f"?{code}", 2) for code in codes])
            except (PJLinkError, OSError, ConnectionError, asyncio.TimeoutError):
                names = []

        inputs = []
        for i, code in enumerate(codes):
            name = names[i] if i < len(names) and isinstance(names[i], str) and names[i] else input_name(code)
            inputs.append({"id": code, "name": name})
        return inputs
