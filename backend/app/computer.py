"""Task-owned Daytona computers. No application credentials enter the sandbox."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import shlex
from uuid import uuid4

from daytona import AsyncDaytona, CreateSandboxFromSnapshotParams, DaytonaConfig, DaytonaNotFoundError, ScreenshotOptions

from app.config import settings
from app.models import AgentTask, TaskSandbox, db, utcnow

logger = logging.getLogger(__name__)


class ComputerUnavailable(RuntimeError):
    """An actionable message safe to show to the agent and user."""


def sandbox_record(task_id: str) -> TaskSandbox | None:
    with db() as session:
        return session.get(TaskSandbox, task_id)


def save_sandbox(task_id: str, **values) -> TaskSandbox:
    with db() as session:
        row = session.get(TaskSandbox, task_id)
        if row is None:
            row = TaskSandbox(task_id=task_id, name=f"band-task-{task_id}")
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


class TaskComputer:
    def __init__(self, task: AgentTask, *, client_factory=AsyncDaytona):
        self.task = task
        self.client_factory = client_factory
        self.client = None
        self.sandbox = None
        self.desktop_started = False
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    def labels(self) -> dict[str, str]:
        workspace = hashlib.sha256(settings.app_secret.encode()).hexdigest()[:16]
        return {"app": "band-slack", "workspace": workspace, "task": self.task.id}

    async def ensure(self, *, desktop: bool = False):
        """Caller holds lock. Persist a deterministic name before allocating cloud resources."""
        if not settings.daytona_api_key:
            raise ComputerUnavailable("Daytona is not configured. Set DAYTONA_API_KEY to enable the computer.")
        if self.sandbox is None:
            if self.client is None:
                self.client = self.client_factory(DaytonaConfig(
                    api_key=settings.daytona_api_key, api_url=settings.daytona_api_url,
                    target=settings.daytona_target or None))
            record = sandbox_record(self.task.id) or save_sandbox(self.task.id)
            if record.status == "deleted":
                raise ComputerUnavailable("This task's computer was deleted. Start a new task for a fresh computer.")
            try:
                sandbox = await self.client.get(record.sandbox_id or record.name, request_timeout=30)
            except DaytonaNotFoundError:
                if record.sandbox_id:
                    save_sandbox(self.task.id, status="missing", frame_id="")
                    raise ComputerUnavailable("The saved Daytona computer has expired or was removed. Start a new task.")
                # A cloud timeout after allocation is recovered by the deterministic name on the next attempt.
                sandbox = await self.client.create(CreateSandboxFromSnapshotParams(
                    name=record.name, snapshot=settings.daytona_snapshot or None,
                    labels=self.labels(), public=False, env_vars={},
                    auto_stop_interval=max(1, settings.daytona_auto_stop),
                    auto_delete_interval=max(1, settings.daytona_auto_delete)), timeout=60)
            if any((sandbox.labels or {}).get(key) != value for key, value in self.labels().items()):
                raise ComputerUnavailable("Sandbox ownership did not match this task. Refusing to attach.")
            # Save identity before start; cancellation/restart can reattach without recreating it.
            save_sandbox(self.task.id, sandbox_id=sandbox.id, status="starting", error=None, frame_id="")
            self.sandbox = sandbox
            state = str(getattr(sandbox.state, "value", sandbox.state))
            if state != "started":
                await sandbox.start(timeout=60)
            save_sandbox(self.task.id, status="running")
        if desktop and not self.desktop_started:
            await self.sandbox.computer_use.start(request_timeout=60)
            info = await self.sandbox.computer_use.display.get_info(request_timeout=15)
            display = next((d for d in info.displays or [] if d.is_active), None)
            if display is None and info.displays:
                display = info.displays[0]
            if display is None or not display.width or not display.height:
                raise ComputerUnavailable("Desktop has no display. Use Daytona's default snapshot or a desktop-ready snapshot.")
            save_sandbox(self.task.id, width=display.width, height=display.height)
            self.desktop_started = True
        return self.sandbox

    async def _capture(self, action: str) -> list[dict]:
        shot = await self.sandbox.computer_use.screenshot.take_compressed(
            ScreenshotOptions(fmt="jpeg", quality=75, scale=1.0, show_cursor=True), request_timeout=20)
        encoded = shot.screenshot or ""
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        if not encoded or len(encoded) > 7_000_000:
            raise ComputerUnavailable("Desktop screenshot is empty or too large.")
        try:
            base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ComputerUnavailable("Daytona returned an invalid screenshot.") from None
        frame = uuid4().hex
        row = save_sandbox(self.task.id, latest_image=f"data:image/jpeg;base64,{encoded}",
                           frame_id=frame, last_action=action, error=None)
        return [
            {"type": "text", "text": json.dumps({"screen_id": frame, "width": row.width,
                "height": row.height, "action": action,
                "instruction": "Use this screen_id for the next action. Coordinates refer to the full image."})},
            {"type": "image_url", "image_url": {"url": row.latest_image}},
        ]

    async def screenshot(self) -> list[dict]:
        async with self.lock:
            await self.ensure(desktop=True)
            return await self._capture("screenshot")

    async def action(self, *, screen_id: str, action: str, x: int = 0, y: int = 0,
                     end_x: int = 0, end_y: int = 0, text: str = "", key: str = "",
                     modifiers: list[str] | None = None, button: str = "left", double: bool = False,
                     direction: str = "down", amount: int = 3, seconds: float = 1) -> list[dict]:
        async with self.lock:
            await self.ensure(desktop=True)
            row = sandbox_record(self.task.id)
            if not screen_id or screen_id != row.frame_id:
                raise ComputerUnavailable("Screen changed or this action was already attempted. Take a new screenshot before acting.")
            if action in {"click", "move", "scroll", "drag"}:
                if not (0 <= x < row.width and 0 <= y < row.height):
                    raise ComputerUnavailable("Coordinates are outside the screenshot.")
            if action == "drag" and not (0 <= end_x < row.width and 0 <= end_y < row.height):
                raise ComputerUnavailable("Drag destination is outside the screenshot.")
            if action not in {"click", "move", "drag", "scroll", "type", "key", "wait"}:
                raise ComputerUnavailable("Unsupported computer action")
            # Consume the frame before the remote side effect. A replay after failure cannot repeat a click/type.
            save_sandbox(self.task.id, frame_id="", last_action=f"Attempting {action}")
            cu = self.sandbox.computer_use
            if action == "click":
                await cu.mouse.click(x, y, button, double, request_timeout=20)
            elif action == "move":
                await cu.mouse.move(x, y, request_timeout=20)
            elif action == "drag":
                await cu.mouse.drag(x, y, end_x, end_y, button, request_timeout=20)
            elif action == "scroll":
                await cu.mouse.scroll(x, y, direction, amount, request_timeout=20)
            elif action == "type":
                await cu.keyboard.type(text, request_timeout=20)
            elif action == "key":
                await cu.keyboard.press(key, modifiers, request_timeout=20)
            elif action == "wait":
                await asyncio.sleep(seconds)
            await asyncio.sleep(0.25)
            return await self._capture(action)

    async def open_browser(self, url: str) -> list[dict]:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or len(url) > 4000:
            raise ComputerUnavailable("Use an http:// or https:// browser URL")
        async with self.lock:
            sandbox = await self.ensure(desktop=True)
            save_sandbox(self.task.id, frame_id="", last_action="Opening browser")
            command = (
                "export DISPLAY=${DISPLAY:-:0}; "
                "browser=$(command -v chromium || command -v chromium-browser || command -v google-chrome || true); "
                # Daytona sandbox networking drops HTTP/3 (QUIC/UDP); Chromium otherwise fails with ERR_QUIC_PROTOCOL_ERROR.
                'if [ -n "$browser" ]; then nohup "$browser" --no-sandbox --no-first-run --disable-quic '
                "--disable-dev-shm-usage --user-data-dir=/tmp/band-browser " + shlex.quote(url) +
                " >/tmp/band-browser.log 2>&1 </dev/null & "
                "elif command -v firefox >/dev/null; then nohup firefox " + shlex.quote(url) +
                " >/tmp/band-browser.log 2>&1 </dev/null & "
                "else echo 'No browser installed in this snapshot'; exit 1; fi"
            )
            result = await sandbox.process.exec(command, timeout=15)
            if result.exit_code != 0:
                raise ComputerUnavailable("No supported browser could be launched. Use a desktop snapshot with Chromium or Firefox.")
            await asyncio.sleep(1)
            return await self._capture("open_browser")

    async def command(self, command: str, *, python: bool = False) -> dict:
        if len(command) > 30000:
            raise ComputerUnavailable("Code or command exceeds 30000 characters")
        # The wrapper runs remotely, not on the host. Bound stdout/stderr before SDK collection.
        argv = ["python3", "-c", command] if python else ["/bin/bash", "-lc", command]
        wrapper = f"""
import json, os, signal, subprocess, threading
argv = {argv!r}
p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
output = {{"stdout": bytearray(), "stderr": bytearray()}}
def drain(stream, name):
    while True:
        block = stream.read(4096)
        if not block:
            return
        remaining = max(0, 12000 - len(output[name]))
        output[name].extend(block[:remaining])
threads = [threading.Thread(target=drain, args=(p.stdout, "stdout"), daemon=True),
           threading.Thread(target=drain, args=(p.stderr, "stderr"), daemon=True)]
for thread in threads:
    thread.start()
timed_out = False
try:
    p.wait(timeout=30)
except subprocess.TimeoutExpired:
    timed_out = True
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.wait()
for thread in threads:
    thread.join(timeout=1)
print(json.dumps({{"exit_code": p.returncode, "timed_out": timed_out,
    "stdout": bytes(output["stdout"]).decode("utf-8", errors="replace"),
    "stderr": bytes(output["stderr"]).decode("utf-8", errors="replace")}}))
"""

        async with self.lock:
            sandbox = await self.ensure()
            save_sandbox(self.task.id, frame_id="", last_action="Running Python" if python else "Running command")
            result = await sandbox.process.exec("python3 -c " + shlex.quote(wrapper), timeout=40)
            if result.exit_code != 0:
                return {"error": "Remote command timed out or failed", "output": (result.result or "")[:12000]}
            return json.loads(result.result)

    async def write_file(self, path: str, content: str) -> dict:
        if not path.startswith("/") or len(path) > 500 or len(content.encode()) > 200000:
            raise ComputerUnavailable("Use an absolute sandbox path and content up to 200000 bytes")
        async with self.lock:
            sandbox = await self.ensure()
            save_sandbox(self.task.id, frame_id="", last_action="Writing file")
            await sandbox.fs.upload_file(content.encode(), path, timeout=30)
            return {"path": path, "bytes": len(content.encode())}

    async def export_file(self, path: str) -> dict:
        if not path.startswith("/") or len(path) > 500:
            raise ComputerUnavailable("Use an absolute sandbox file path")
        code = f"import base64;print(base64.b64encode(open({path!r},'rb').read(200001)).decode())"
        async with self.lock:
            sandbox = await self.ensure()
            response = await sandbox.process.exec("python3 -c " + shlex.quote(code), timeout=15)
            if response.exit_code != 0:
                raise ComputerUnavailable("Could not read the sandbox file")
            data = base64.b64decode(response.result.strip(), validate=True)
            if len(data) > 200000:
                raise ComputerUnavailable("Export supports text files up to 200000 bytes")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ComputerUnavailable("Export currently supports UTF-8 text files") from None
            with db() as session:
                task = session.get(AgentTask, self.task.id)
                artifacts = [a for a in task.artifacts if a["path"] != path]
                if len(artifacts) >= 20 or sum(len(a["content"]) for a in artifacts) + len(content) > 200000:
                    raise ComputerUnavailable("Task export limit reached (20 files / 200000 characters)")
                task.artifacts = [*artifacts, {"path": path, "content": content}]
                session.add(task)
                session.commit()
            return {"path": path, "bytes": len(data), "download": "Available in task results"}

    async def close(self) -> None:
        # Also recover an allocation made just before a worker crash, even if the graph
        # is already complete and never calls a computer tool on its resumed attempt.
        try:
            record = sandbox_record(self.task.id)
            if self.sandbox is None and record and settings.daytona_api_key and record.status in {
                "running", "starting", "provisioning", "stop_failed"
            }:
                try:
                    if self.client is None:
                        self.client = self.client_factory(DaytonaConfig(
                            api_key=settings.daytona_api_key, api_url=settings.daytona_api_url,
                            target=settings.daytona_target or None))
                    sandbox = await self.client.get(record.sandbox_id or record.name, request_timeout=10)
                    if any((sandbox.labels or {}).get(key) != value for key, value in self.labels().items()):
                        raise ComputerUnavailable("Sandbox ownership did not match this task")
                    self.sandbox = sandbox
                    save_sandbox(self.task.id, sandbox_id=sandbox.id)
                except DaytonaNotFoundError:
                    save_sandbox(self.task.id, status="missing", frame_id="")
                except Exception:
                    logger.warning("Could not recover task computer %s for cleanup", self.task.id)
                    save_sandbox(self.task.id, status="stop_failed", frame_id="",
                                 error="Cleanup was not confirmed; check Daytona dashboard")
            if self.sandbox is not None:
                try:
                    state = str(getattr(self.sandbox.state, "value", self.sandbox.state))
                    if state != "stopped":
                        await self.sandbox.stop(timeout=20)
                    save_sandbox(self.task.id, status="stopped", frame_id="")
                except Exception:
                    logger.warning("Could not stop task computer %s", self.task.id)
                    save_sandbox(self.task.id, status="stop_failed", frame_id="",
                                 error="Stop was not confirmed; check Daytona dashboard")
        finally:
            if self.client is not None:
                try:
                    await self.client.close()
                except Exception:
                    logger.warning("Could not close Daytona client for task %s", self.task.id)
                self.client = None
            self.sandbox = None
            self.desktop_started = False
