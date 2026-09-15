from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from .config import ExecutorConfig, GatewayConfig
from .models import DelegationRequest, DelegationResult


class ExecutorUnavailable(RuntimeError):
    """Executor cannot currently accept work."""


@dataclass(slots=True)
class Executor:
    config: ExecutorConfig
    gateway_config: GatewayConfig

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def capabilities(self) -> set[str]:
        return self.config.capabilities

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        raise NotImplementedError

    def supports(self, request: DelegationRequest) -> bool:
        required = set(request.capabilities)
        return self.config.enabled and required.issubset(self.capabilities)

    def _truncate(self, value: str) -> str:
        limit = self.gateway_config.max_output_chars
        if len(value) <= limit:
            return value
        omitted = len(value) - limit
        return value[:limit] + f"\n...[truncated {omitted} characters]"


class EchoExecutor(Executor):
    async def execute(self, request: DelegationRequest) -> DelegationResult:
        result = DelegationResult.queued(request.id)
        result.mark_started(self.name)
        result.stdout = request.task
        result.payload = {"echo": request.task}
        result.mark_completed(ok=True, exit_code=0)
        return result


class AgentProcessExecutor(Executor):
    """Run an agent CLI and pass the natural-language task over stdin.

    No shell is involved: argv is executed directly. This makes adapters portable and
    prevents shell metacharacters in a delegated task from becoming commands by accident.
    """

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        if not self.config.argv:
            raise ExecutorUnavailable(f"executor {self.name!r} has no argv")

        result = DelegationResult.queued(request.id)
        result.mark_started(self.name)
        cwd = self.config.cwd
        if cwd and not Path(cwd).exists():
            raise ExecutorUnavailable(f"executor cwd does not exist: {cwd}")

        try:
            process = await asyncio.create_subprocess_exec(
                *self.config.argv,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ExecutorUnavailable(str(exc)) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(request.task.encode("utf-8")),
                timeout=request.timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            result.stderr = f"executor timed out after {request.timeout_seconds}s"
            result.error = "timeout"
            result.mark_completed(ok=False)
            return result

        result.stdout = self._truncate(stdout_b.decode("utf-8", errors="replace"))
        result.stderr = self._truncate(stderr_b.decode("utf-8", errors="replace"))
        result.mark_completed(ok=process.returncode == 0, exit_code=process.returncode)
        if process.returncode != 0:
            result.error = f"executor exited with code {process.returncode}"
        return result


class StructuredProcessExecutor(Executor):
    """Execute an explicitly structured argv action, never a shell string."""

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        if request.action is None or request.action.kind != "process":
            raise ExecutorUnavailable("structured process executor requires action.kind=process")
        if not request.action.argv:
            raise ExecutorUnavailable("empty process argv")
        command = request.action.argv[0]
        allowed = self.config.allowed_commands
        if not allowed:
            raise ExecutorUnavailable("structured process executor has no allowed_commands policy")
        command_name = os.path.basename(command)
        is_bare_command = command_name == command
        if (is_bare_command and command_name not in allowed) or (
            not is_bare_command and command not in allowed
        ):
            raise ExecutorUnavailable(f"command {command!r} is not allowlisted")

        unexpected_env = set(request.action.env) - self.config.allowed_env
        if unexpected_env:
            names = ", ".join(sorted(unexpected_env))
            raise ExecutorUnavailable(f"environment keys are not allowlisted: {names}")

        result = DelegationResult.queued(request.id)
        result.mark_started(self.name)
        env = os.environ.copy()
        env.update(request.action.env)

        try:
            process = await asyncio.create_subprocess_exec(
                *request.action.argv,
                cwd=request.action.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ExecutorUnavailable(str(exc)) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(), timeout=request.timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            result.error = "timeout"
            result.stderr = f"process timed out after {request.timeout_seconds}s"
            result.mark_completed(ok=False)
            return result

        result.stdout = self._truncate(stdout_b.decode("utf-8", errors="replace"))
        result.stderr = self._truncate(stderr_b.decode("utf-8", errors="replace"))
        result.mark_completed(ok=process.returncode == 0, exit_code=process.returncode)
        if process.returncode != 0:
            result.error = f"process exited with code {process.returncode}"
        return result


def build_executors(config: GatewayConfig) -> list[Executor]:
    kinds = {
        "echo": EchoExecutor,
        "agent_process": AgentProcessExecutor,
        "structured_process": StructuredProcessExecutor,
    }
    output: list[Executor] = []
    for executor_config in config.executors:
        cls = kinds.get(executor_config.type)
        if cls is None:
            continue
        output.append(cls(executor_config, config))
    return output
