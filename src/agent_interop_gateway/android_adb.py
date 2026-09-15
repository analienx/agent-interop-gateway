from __future__ import annotations

import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


class AdbError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UiNode:
    text: str
    content_desc: str
    class_name: str
    resource_id: str
    package: str
    editable: bool
    clickable: bool
    bounds: tuple[int, int, int, int] | None

    @property
    def center(self) -> tuple[int, int] | None:
        if not self.bounds:
            return None
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


class AndroidAdb:
    """Headless Android UI bridge using an explicitly authorized ADB connection."""

    def __init__(self, adb: str = "adb", serial: str | None = None) -> None:
        self.adb = adb
        self.serial = serial

    def _base(self) -> list[str]:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, *args: str, timeout: float = 15) -> str:
        try:
            completed = subprocess.run(
                [*self._base(), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise AdbError(str(exc)) from exc
        if completed.returncode != 0:
            raise AdbError(completed.stderr.strip() or completed.stdout.strip())
        return completed.stdout

    def current_package(self) -> str | None:
        output = self.run("shell", "dumpsys", "window", "windows")
        match = re.search(r"mCurrentFocus=.*?\s([A-Za-z0-9._]+)/", output)
        return match.group(1) if match else None

    def dump_ui(self) -> list[UiNode]:
        remote = "/sdcard/aigw-window.xml"
        self.run("shell", "uiautomator", "dump", remote)
        xml = self.run("exec-out", "cat", remote)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise AdbError(f"invalid UI hierarchy XML: {exc}") from exc
        return [self._node_from_element(element) for element in root.iter("node")]

    def _node_from_element(self, element: ET.Element) -> UiNode:
        attrs = element.attrib
        bounds = None
        match = BOUNDS.fullmatch(attrs.get("bounds", ""))
        if match:
            bounds = tuple(int(value) for value in match.groups())  # type: ignore[assignment]
        return UiNode(
            text=attrs.get("text", ""),
            content_desc=attrs.get("content-desc", ""),
            class_name=attrs.get("class", ""),
            resource_id=attrs.get("resource-id", ""),
            package=attrs.get("package", ""),
            editable=attrs.get("editable") == "true",
            clickable=attrs.get("clickable") == "true",
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
                    "bounds": node.bounds,
                }
            )
        return rows

    def tap(self, x: int, y: int) -> None:
        self.run("shell", "input", "tap", str(x), str(y))

    def input_text(self, text: str) -> None:
        escaped = text.replace("%", "%25").replace(" ", "%s")
        self.run("shell", "input", "text", escaped, timeout=30)

    def find_editor(self, nodes: list[UiNode] | None = None) -> UiNode | None:
        nodes = nodes or self.dump_ui()
        candidates = [node for node in nodes if node.editable]
        return candidates[-1] if candidates else None

    def find_send(self, nodes: list[UiNode] | None = None) -> UiNode | None:
        nodes = nodes or self.dump_ui()
        terms = {"send", "odeslat", "odeslat zprávu"}
        for node in reversed(nodes):
            label = f"{node.text} {node.content_desc}".strip().lower()
            if node.clickable and any(term in label for term in terms):
                return node
        return None

    def inject_text_message(self, text: str, settle_seconds: float = 0.25) -> None:
        nodes = self.dump_ui()
        editor = self.find_editor(nodes)
        if editor is None or editor.center is None:
            raise AdbError("no editable message field is exposed in the current UI")
        self.tap(*editor.center)
        self.input_text(text)
        time.sleep(settle_seconds)
        nodes = self.dump_ui()
        send = self.find_send(nodes)
        if send and send.center:
            self.tap(*send.center)
            return
        self.run("shell", "input", "keyevent", "66")

    def screenshot(self, destination: str | Path) -> Path:
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
