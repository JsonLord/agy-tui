"""Pinned capability contracts, including explicit source-proven safety blockers."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .policy import HybridError

SOURCE_2X = "https://github.com/anomalyco/opencode/tree/026b106bc5d41b679ffc413264229c340871e4d3"


@dataclass(frozen=True)
class OpenCodeCapabilities:
    version: str
    generation: str
    cwd_mode: str
    transport_mode: str
    bind_control: str
    purity_mode: str
    session_mode: str
    worker_blockers: tuple[str, ...] = ()

    def describe(self) -> dict:
        return {
            **asdict(self),
            "worker_ready": not self.worker_blockers,
            "source_evidence": SOURCE_2X
            if self.generation == "2.x"
            else "legacy ACP contract + installed help",
        }

    def acp_arguments(self, root) -> tuple[str, ...]:
        if not root.is_absolute():
            raise HybridError("WORKSPACE_CWD_NOT_ABSOLUTE")
        if self.generation == "1.x":
            return (
                "acp",
                "--pure",
                "--hostname",
                "127.0.0.1",
                "--port",
                "0",
                "--cwd",
                str(root),
            )
        return (
            "acp",
        )  # Source-proven candidate; require_worker() prevents unsafe activation.

    def require_worker(self):
        if self.worker_blockers:
            raise HybridError(self.worker_blockers[0])


def validate_version(version: str):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise HybridError("OPENCODE_VERSION_UNOBSERVABLE")
    if version not in {"1.18.35", "2.0.28"}:
        raise HybridError("OPENCODE_CAPABILITIES_UNVERIFIED")


def detect_capabilities(version: str, help_text: str) -> OpenCodeCapabilities:
    validate_version(version)
    flags = ("--cwd", "--pure", "--hostname", "--port")
    if version == "1.18.35":
        if not all(flag in help_text for flag in flags):
            raise HybridError("ACP_FLAGS")
        return OpenCodeCapabilities(
            version,
            "1.x",
            "os_cwd_and_cli_and_session",
            "stdio_with_loopback_server",
            "explicit_loopback_ephemeral",
            "external_plugins_disabled",
            "managed_native_sessions",
        )
    if version == "2.0.28":
        if "--login" not in help_text or any(flag in help_text for flag in flags):
            raise HybridError("ACP_CAPABILITY_CONTRACT_MISMATCH")
        return OpenCodeCapabilities(
            version,
            "2.x",
            "os_cwd_and_absolute_acp_session",
            "stdio_with_private_http_child",
            "source_default_127.0.0.1_port_0",
            "native_plugins_no_universal_pure",
            "native_database_shared_with_credentials",
            ("OPENCODE_NATIVE_STATE_MUTATION", "OPENCODE_PLUGIN_ISOLATION_UNVERIFIED"),
        )
    raise HybridError("OPENCODE_CAPABILITIES_UNVERIFIED")


async def probe_capabilities(
    binary: str, environment: dict, cwd
) -> OpenCodeCapabilities:
    """Only version/help, never starts an ACP session/server or reads a credential file."""
    import asyncio

    async def output(*arguments):
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                *arguments,
                cwd=cwd,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise HybridError("OPENCODE_EXECUTABLE_BLOCKED") from None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise HybridError("OPENCODE_EXECUTABLE_BLOCKED")
        if len(stdout) + len(stderr) > 256 * 1024:
            raise HybridError("OPENCODE_PROBE_OUTPUT_LIMIT")
        return (stdout + stderr).decode("utf-8", errors="replace")

    version = (await output("--version")).strip()
    validate_version(version)
    return detect_capabilities(version, await output("acp", "--help"))
