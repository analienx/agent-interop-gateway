from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import ExecutorConfig, GatewayConfig
from .models import DelegationRequest, DelegationResult, ExecutorAttempt, Risk


class ExecutorUnavailable(RuntimeError):
    """Executor cannot currently accept work."""


@dataclass(slots=True)
class ProcessOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(slots=True)
class Executor:
    config: ExecutorConfig
    gateway_config: GatewayConfig
    _semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.config.max_parallel)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def capabilities(self) -> set[str]:
        return self.config.capabilities

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        raise NotImplementedError

    async def probe(self) -> tuple[bool, str | None]:
        return True, None

    def supports(self, request: DelegationRequest) -> bool:
        required = set(request.capabilities)
        return (
            self.config.enabled
            and request.risk.value in self.config.allowed_risks
            and required.issubset(self.capabilities)
        )

    def _truncate(self, value: str) -> str:
        limit = self.gateway_config.max_output_chars
        if len(value) <= limit:
            return value
        omitted = len(value) - limit
        return value[:limit] + f"\n...[truncated {omitted} characters]"

    def _environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        if self.config.inherit_env:
            env = os.environ.copy()
        else:
            baseline = {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
                "LOCALAPPDATA",
                "APPDATA",
                "LANG",
                "LC_ALL",
            }
            env = {key: value for key, value in os.environ.items() if key.upper() in baseline}
            for key in self.config.allowed_env:
                if key in os.environ:
                    env[key] = os.environ[key]
        if extra:
            env.update(extra)
        return env

    def _is_transient(self, outcome: ProcessOutcome) -> bool:
        if outcome.timed_out:
            return True
        if outcome.returncode in self.config.retry_exit_codes:
            return True
        return any(
            re.search(pattern, outcome.stderr, re.IGNORECASE)
            for pattern in self.config.transient_stderr_patterns
        )


class EchoExecutor(Executor):
    async def execute(self, request: DelegationRequest) -> DelegationResult:
        result = DelegationResult.queued(request.id)
        result.mark_started(self.name)
        started = datetime.now(UTC)
        result.stdout = self._truncate(request.task)
        result.payload = {"echo": request.task}
        result.mark_completed(ok=True, exit_code=0)
        result.attempts.append(
            ExecutorAttempt(
                executor=self.name,
                attempt=1,
                started_at=started,
                completed_at=datetime.now(UTC),
                exit_code=0,
            )
        )
        return result


class ProcessExecutor(Executor):
    async def _execute_process(
        self,
        request: DelegationRequest,
        argv: list[str],
        *,
        cwd: str | None,
        env: dict[str, str],
        stdin_text: str | None,
    ) -> DelegationResult:
        result = DelegationResult.queued(request.id)
        result.mark_started(self.name)
        attempts = self.config.max_attempts if request.risk == Risk.READ else 1

        for attempt_number in range(1, attempts + 1):
            started = datetime.now(UTC)
            outcome = await _run_process(
                argv,
                cwd=cwd,
                env=env,
                stdin_text=stdin_text,
                timeout=float(request.timeout_seconds),
                output_char_limit=self.gateway_config.max_output_chars,
                kill_grace_seconds=self.config.kill_grace_seconds,
            )
            transient = self._is_transient(outcome)
            error = None
            if outcome.timed_out:
                error = f"executor timed out after {request.timeout_seconds}s"
            elif outcome.returncode != 0:
                error = f"executor exited with code {outcome.returncode}"

            result.attempts.append(
                ExecutorAttempt(
                    executor=self.name,
                    attempt=attempt_number,
                    started_at=started,
                    completed_at=datetime.now(UTC),
                    exit_code=outcome.returncode,
                    error=error,
                    transient=transient,
                )
            )
            result.stdout = outcome.stdout
            result.stderr = outcome.stderr
            result.exit_code = outcome.returncode

            if not outcome.timed_out and outcome.returncode == 0:
                result.mark_completed(ok=True, exit_code=0)
                return result

            if attempt_number < attempts and transient:
                delay = self.config.retry_backoff_seconds * (2 ** (attempt_number - 1))
                if delay:
                    await asyncio.sleep(delay)
                continue

            result.error = error or "executor failed"
            result.mark_completed(ok=False, exit_code=outcome.returncode)
            return result

        raise AssertionError("unreachable")

    async def probe(self) -> tuple[bool, str | None]:
        if not self.config.argv:
            return False, "argv is empty"
        executable = self.config.argv[0]
        if os.path.isabs(executable):
            if not Path(executable).is_file():
                return False, f"executable does not exist: {executable}"
        elif shutil.which(executable) is None:
            return False, f"executable not found on PATH: {executable}"
        if self.config.cwd and not Path(self.config.cwd).is_dir():
            return False, f"cwd does not exist: {self.config.cwd}"
        return True, None


class AgentProcessExecutor(ProcessExecutor):
    """Run a configured agent CLI and pass the natural-language task over stdin."""

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        if not self.config.argv:
            raise ExecutorUnavailable(f"executor {self.name!r} has no argv")
        if self.config.cwd and not Path(self.config.cwd).is_dir():
            raise ExecutorUnavailable(f"executor cwd does not exist: {self.config.cwd}")
        ok, reason = await self.probe()
        if not ok:
            raise ExecutorUnavailable(reason or "executor probe failed")
        async with self._semaphore:
            return await self._execute_process(
                request,
                self.config.argv,
                cwd=self.config.cwd,
                env=self._environment(),
                stdin_text=request.task,
            )


class StructuredProcessExecutor(ProcessExecutor):
    """Execute an explicitly structured argv action, never a shell string."""

    async def execute(self, request: DelegationRequest) -> DelegationResult:
        if request.action is None or request.action.kind != "process":
            raise ExecutorUnavailable("structured process executor requires action.kind=process")
        argv = list(request.action.argv)
        argv[0] = self._resolve_command(argv[0])
        self._validate_cwd(request.action.cwd)

        unexpected_env = set(request.action.env) - self.config.allowed_env
        if unexpected_env:
            names = ", ".join(sorted(unexpected_env))
            raise ExecutorUnavailable(f"environment keys are not allowlisted: {names}")

        async with self._semaphore:
            return await self._execute_process(
                request,
                argv,
                cwd=request.action.cwd,
                env=self._environment(request.action.env),
                stdin_text=None,
            )

    def _resolve_command(self, command: str) -> str:
        if not self.config.allowed_commands:
            raise ExecutorUnavailable("structured process executor has no allowed_commands policy")
        has_path = (
            os.path.isabs(command) or os.sep in command or (os.altsep and os.altsep in command)
        )
        if has_path:
            candidate = os.path.normcase(os.path.realpath(command))
            allowed = {
                os.path.normcase(os.path.realpath(item))
                for item in self.config.allowed_commands
                if os.path.isabs(item) or os.sep in item or (os.altsep and os.altsep in item)
            }
            if candidate not in allowed:
                raise ExecutorUnavailable(
                    f"command {command!r} is not allowlisted by canonical path"
                )
            if not Path(command).is_file():
                raise ExecutorUnavailable(f"command does not exist: {command}")
            return command

        if not self.config.allow_path_lookup:
            raise ExecutorUnavailable("bare command lookup is disabled; allowlist an absolute path")
        if command not in self.config.allowed_commands:
            raise ExecutorUnavailable(f"command {command!r} is not allowlisted")
        resolved = shutil.which(command)
        if resolved is None:
            raise ExecutorUnavailable(f"command {command!r} was not found on PATH")
        return resolved

    def _validate_cwd(self, cwd: str | None) -> None:
        if cwd is None:
            return
        target = Path(cwd).resolve()
        if not target.is_dir():
            raise ExecutorUnavailable(f"cwd does not exist: {cwd}")
        if not self.config.allowed_cwds:
            raise ExecutorUnavailable("request-supplied cwd is disabled; configure allowed_cwds")
        roots = [Path(item).expanduser().resolve() for item in self.config.allowed_cwds]
        if not any(_is_relative_to(target, root) for root in roots):
            raise ExecutorUnavailable(f"cwd is outside allowed_cwds: {cwd}")

    async def probe(self) -> tuple[bool, str | None]:
        return True, None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


async def _read_limited(stream: asyncio.StreamReader | None, char_limit: int) -> tuple[str, int]:
    if stream is None:
        return "", 0
    byte_limit = max(char_limit * 4, 4096)
    chunks: list[bytes] = []
    retained = 0
    omitted = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        remaining = max(0, byte_limit - retained)
        if remaining:
            kept = chunk[:remaining]
            chunks.append(kept)
            retained += len(kept)
        omitted += max(0, len(chunk) - remaining)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if len(text) > char_limit:
        omitted += len(text[char_limit:].encode("utf-8", errors="replace"))
        text = text[:char_limit]
    if omitted:
        text += f"\n...[truncated approximately {omitted} bytes]"
    return text, omitted


async def _run_process(
    argv: list[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    stdin_text: str | None,
    timeout: float,
    output_char_limit: int,
    kill_grace_seconds: float,
) -> ProcessOutcome:
    try:
        stdin = asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL
        if os.name == "nt":
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise ExecutorUnavailable(str(exc)) from exc

    stdout_task = asyncio.create_task(_read_limited(process.stdout, output_char_limit))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, output_char_limit))
    timed_out = False
    cancelled = False

    async def feed_and_wait() -> None:
        if stdin_text is not None and process.stdin is not None:
            process.stdin.write(stdin_text.encode("utf-8"))
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.drain()
            process.stdin.close()
        await process.wait()

    try:
        # The deadline covers stdin backpressure as well as process execution.
        await asyncio.wait_for(feed_and_wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        await _terminate_tree(process, kill_grace_seconds)
    except asyncio.CancelledError:
        cancelled = True
        await asyncio.shield(_terminate_tree(process, kill_grace_seconds))
    finally:
        stdout, _ = await stdout_task
        stderr, _ = await stderr_task

    if cancelled:
        raise asyncio.CancelledError

    return ProcessOutcome(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
    )


async def _terminate_tree(process: asyncio.subprocess.Process, grace_seconds: float) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            gentle = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await gentle.wait()
        except OSError:
            process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
        except ProcessLookupError:
            return

    try:
        await asyncio.wait_for(process.wait(), timeout=max(grace_seconds, 0.1))
        return
    except TimeoutError:
        pass

    if os.name == "nt":
        try:
            force = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await force.wait()
        except OSError:
            process.kill()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
    await process.wait()


def build_executors(config: GatewayConfig) -> list[Executor]:
    kinds = {
        "echo": EchoExecutor,
        "agent_process": AgentProcessExecutor,
        "structured_process": StructuredProcessExecutor,
    }
    return [kinds[item.type](item, config) for item in config.executors]
