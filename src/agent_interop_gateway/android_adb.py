from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
SAFE_ADB_TEXT = re.compile(r"^[ -~]+$")


class AdbError(RuntimeError):
    pass


class InjectionUnavailable(AdbError):
    """No message-changing action was issued; retrying later is safe."""


class InjectionUncertain(AdbError):
    """A message-changing action was issued but delivery could not be confirmed."""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    serial: str
    state: str
    details: str = ""


@dataclass(frozen=True, slots=True)
class UiNode:
    text: str
    content_desc: str
    class_name: str
    resource_id: str
    package: str
    editable: bool
    clickable: bool
    enabled: bool
    focused: bool
    focusable: bool
    bounds: tuple[int, int, int, int] | None

    @property
    def center(self) -> tuple[int, int] | None:
        if not self.bounds:
            return None
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


@dataclass(frozen=True, slots=True)
class InjectionReceipt:
    confirmed: bool
    send_method: str


class AndroidAdb:
    """Headless Android UI bridge using an explicitly authorized ADB connection."""

    def __init__(self, adb: str = "adb", serial: str | None = None) -> None:
        self.adb = adb
        self.serial = serial
        self._device_checked_at = 0.0

    def _base(self) -> list[str]:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def _run_raw(self, *args: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.adb, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"ADB executable not found: {self.adb}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB command timed out after {timeout}s") from exc

    def list_devices(self) -> list[DeviceInfo]:
        completed = self._run_raw("devices", "-l")
        if completed.returncode != 0:
            raise AdbError(completed.stderr.strip() or completed.stdout.strip())
        devices: list[DeviceInfo] = []
        for raw in completed.stdout.splitlines()[1:]:
            line = raw.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split(maxsplit=2)
            if len(parts) < 2:
                continue
            devices.append(
                DeviceInfo(
                    serial=parts[0],
                    state=parts[1],
                    details=parts[2] if len(parts) > 2 else "",
                )
            )
        return devices

    def ensure_device(self, *, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self.serial and now - self._device_checked_at < 5:
            return self.serial
        devices = self.list_devices()
        if self.serial:
            match = next((item for item in devices if item.serial == self.serial), None)
            if match is None:
                raise AdbError(f"ADB device {self.serial!r} is not connected")
            if match.state != "device":
                raise AdbError(f"ADB device {self.serial!r} state is {match.state!r}")
        else:
            authorized = [item for item in devices if item.state == "device"]
            blocked = [item for item in devices if item.state != "device"]
            if not authorized:
                if blocked:
                    states = ", ".join(f"{item.serial}:{item.state}" for item in blocked)
                    raise AdbError(f"no authorized ADB device; detected {states}")
                raise AdbError("no ADB device connected")
            if len(authorized) > 1:
                serials = ", ".join(item.serial for item in authorized)
                raise AdbError(f"multiple ADB devices connected; pass --serial ({serials})")
            self.serial = authorized[0].serial
        self._device_checked_at = now
        return self.serial

    def run(self, *args: str, timeout: float = 15) -> str:
        self.ensure_device()
        try:
            completed = subprocess.run(
                [*self._base(), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"ADB executable not found: {self.adb}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB command timed out after {timeout}s") from exc
        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip() or "unknown ADB failure"
            if "device" in error.lower() or "transport" in error.lower():
                self._device_checked_at = 0
            raise AdbError(error)
        return completed.stdout

    def current_package(self) -> str | None:
        probes = [
            (
                ("shell", "dumpsys", "window", "windows"),
                [
                    r"mCurrentFocus=.*?\s([A-Za-z0-9._]+)/",
                    r"mFocusedApp=.*?\s([A-Za-z0-9._]+)/",
                ],
            ),
            (
                ("shell", "dumpsys", "activity", "activities"),
                [
                    r"mResumedActivity:.*?\s([A-Za-z0-9._]+)/",
                    r"topResumedActivity=.*?\s([A-Za-z0-9._]+)/",
                ],
            ),
        ]
        for args, patterns in probes:
            output = self.run(*args)
            for pattern in patterns:
                match = re.search(pattern, output)
                if match:
                    return match.group(1)
        return None

    def dump_ui(self, retries: int = 3) -> list[UiNode]:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            remote = f"/sdcard/aigw-window-{os.getpid()}-{time.monotonic_ns()}.xml"
            try:
                self.run("shell", "uiautomator", "dump", remote, timeout=20)
                xml = self.run("exec-out", "cat", remote, timeout=20)
                root = ET.fromstring(xml)
                return [self._node_from_element(element) for element in root.iter("node")]
            except (ET.ParseError, AdbError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(0.15 * attempt)
            finally:
                with contextlib.suppress(AdbError):
                    self.run("shell", "rm", "-f", remote, timeout=5)
        raise AdbError(f"unable to capture stable UI hierarchy: {last_error}")

    def _node_from_element(self, element: ET.Element) -> UiNode:
        attrs = element.attrib
        bounds = None
        match = BOUNDS.fullmatch(attrs.get("bounds", ""))
        if match:
            bounds = (
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
            )
        return UiNode(
            text=attrs.get("text", ""),
            content_desc=attrs.get("content-desc", ""),
            class_name=attrs.get("class", ""),
            resource_id=attrs.get("resource-id", ""),
            package=attrs.get("package", ""),
            editable=attrs.get("editable") == "true",
            clickable=attrs.get("clickable") == "true",
            enabled=attrs.get("enabled", "true") == "true",
            focused=attrs.get("focused") == "true",
            focusable=attrs.get("focusable") == "true",
            bounds=bounds,
        )

    def semantic_snapshot(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for node in self.dump_ui():
            if not (node.text or node.content_desc or node.editable):
                continue
            rows.append(
                {
                    "text": node.text,
                    "content_desc": node.content_desc,
                    "class": node.class_name,
                    "resource_id": node.resource_id,
                    "editable": node.editable,
                    "clickable": node.clickable,
                    "enabled": node.enabled,
                    "focused": node.focused,
                    "bounds": node.bounds,
                }
            )
        return rows

    def tap(self, x: int, y: int) -> None:
        self.run("shell", "input", "tap", str(x), str(y))

    def input_text(self, text: str) -> None:
        if not text:
            raise AdbError("refusing to inject empty text")
        if not SAFE_ADB_TEXT.fullmatch(text):
            raise AdbError(
                "ADB text injection only supports conservative ASCII safely; "
                "use the Android companion bridge for arbitrary text"
            )
        escaped = text.replace("%", "%25").replace(" ", "%s")
        # adb shell joins trailing arguments into an Android shell command. Quote the
        # input as one remote-shell token so punctuation cannot become shell syntax.
        self.run("shell", "input", "text", shlex.quote(escaped), timeout=30)

    def find_editor(self, nodes: list[UiNode] | None = None) -> UiNode | None:
        nodes = nodes or self.dump_ui()
        candidates = [node for node in nodes if node.editable and node.enabled and node.center]
        if not candidates:
            return None
        candidates.sort(
            key=lambda node: (
                int(node.focused),
                int("edit" in node.class_name.lower()),
                node.bounds[3] if node.bounds else -1,
            )
        )
        return candidates[-1]

    def find_send(self, nodes: list[UiNode] | None = None) -> UiNode | None:
        nodes = nodes or self.dump_ui()
        terms = {"send", "send message", "odeslat", "odeslat zprávu"}
        candidates: list[tuple[int, UiNode]] = []
        for node in nodes:
            if not (node.clickable and node.enabled and node.center):
                continue
            label = f"{node.text} {node.content_desc}".strip().lower()
            resource = node.resource_id.lower()
            score = 0
            if any(term == label for term in terms):
                score += 4
            if any(term in label for term in terms):
                score += 2
            if "send" in resource:
                score += 3
            if score:
                candidates.append((score, node))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1].bounds[3] if item[1].bounds else -1))
        return candidates[-1][1]

    def inject_text_message(
        self,
        text: str,
        *,
        expected_package: str | None = None,
        settle_seconds: float = 0.25,
        verify_seconds: float = 3.0,
    ) -> InjectionReceipt:
        if not text or not SAFE_ADB_TEXT.fullmatch(text):
            raise InjectionUnavailable(
                "ADB injection requires conservative ASCII; use the Android companion "
                "for arbitrary Unicode results"
            )
        if expected_package and self.current_package() != expected_package:
            raise InjectionUnavailable(f"expected foreground package {expected_package!r}")
        nodes = self.dump_ui()
        editor = self.find_editor(nodes)
        if editor is None or editor.center is None:
            raise InjectionUnavailable(
                "no enabled editable message field is exposed in the current UI"
            )
        self.tap(*editor.center)
        self.input_text(text)
        time.sleep(settle_seconds)
        nodes = self.dump_ui()
        send = self.find_send(nodes)
        method = "send-button" if send and send.center else "ime-enter"
        if send and send.center:
            self.tap(*send.center)
        else:
            self.run("shell", "input", "keyevent", "66")

        probe = text[: min(48, len(text))]
        deadline = time.monotonic() + max(verify_seconds, 0)
        while time.monotonic() < deadline:
            time.sleep(0.2)
            try:
                if any(
                    probe in node.text and not node.editable for node in self.dump_ui() if node.text
                ):
                    return InjectionReceipt(confirmed=True, send_method=method)
            except AdbError:
                continue
        raise InjectionUncertain(
            "send action was issued but message delivery was not confirmed; "
            "automatic retry is intentionally disabled"
        )

    def screenshot(self, destination: str | Path) -> Path:
        self.ensure_device()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            command = [*self._base(), "exec-out", "screencap", "-p"]
            completed = subprocess.run(command, capture_output=True, timeout=15, check=False)
            if completed.returncode != 0:
                raise AdbError(completed.stderr.decode(errors="replace"))
            temp_path.write_bytes(completed.stdout)
            destination.write_bytes(temp_path.read_bytes())
            return destination
        finally:
            temp_path.unlink(missing_ok=True)
