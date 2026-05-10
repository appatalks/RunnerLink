#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pty
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, simpledialog, ttk


APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
CONFIG_PATH = APP_DIR / "config.json"
EXAMPLE_CONFIG_PATH = APP_DIR / "config.example.json"
TUNNEL_PORT_MIN = 2222
TUNNEL_PORT_MAX = 2231
ACTIVE_RUN_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
COPILOT_SETUP_COMMAND = "gh copilot"
TERMINAL_AUTH_MODULES = {"azure", "adx"}
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:\][^\a]*(?:\a|\x1B\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")
THEME = {
    "app_bg": "#0b1120",
    "panel_bg": "#111827",
    "panel_alt": "#0f172a",
    "border": "#334155",
    "text": "#e5e7eb",
    "muted": "#94a3b8",
    "status": "#cbd5e1",
    "activity_bg": "#020617",
    "activity_fg": "#d1d5db",
    "primary_fill": "#0f766e",
    "primary_line": "#5eead4",
    "neutral_fill": "#1e293b",
    "neutral_line": "#94a3b8",
    "danger_fill": "#7f1d1d",
    "danger_line": "#fca5a5",
}

COPILOT_INSTALL_SCRIPT = r"""
set -u
echo 'checking GitHub Copilot CLI'
if GH_PROMPT_DISABLED=1 gh copilot -- --help >/dev/null 2>&1; then
    echo 'copilot command ready'
    exit 0
fi

echo 'removing stale Copilot extension'
gh extension remove gh-copilot >/dev/null 2>&1 || gh extension remove github/gh-copilot >/dev/null 2>&1 || true

echo 'installing GitHub Copilot CLI'
if ! GH_PROMPT_DISABLED=1 gh extension install github/gh-copilot; then
    echo 'direct extension install failed; trying gh copilot installer shim'
    if command -v python3 >/dev/null 2>&1; then
        python3 - <<'PY' || true
import os
import pty
import select
import signal
import subprocess
import sys
import time

def copilot_help_ready():
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    return subprocess.run(
        ["gh", "copilot", "--", "--help"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        check=False,
    ).returncode == 0


def stop_child(child_pid, signal_number=signal.SIGTERM):
    try:
        os.kill(child_pid, signal_number)
    except ProcessLookupError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            done_pid, _ = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return
        if done_pid == child_pid:
            return
        time.sleep(0.1)
    try:
        os.kill(child_pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        os.waitpid(child_pid, 0)
    except ChildProcessError:
        pass


pid, fd = pty.fork()
if pid == 0:
    os.execvp("gh", ["gh", "copilot"])

deadline = time.time() + 120
sent_yes = False
sent_yes_at = 0.0
last_help_check = 0.0
captured = []
status = None

while time.time() < deadline:
    readable, _, _ = select.select([fd], [], [], 0.2)
    if fd in readable:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        text = data.decode("utf-8", errors="replace")
        captured.append(text)
        captured = captured[-40:]
        sys.stdout.write(text)
        sys.stdout.flush()
        tail = "".join(captured)[-4000:].lower()
        if not sent_yes and (
            "would you like to install" in tail
            or "type yes" in tail
            or "(y/n)" in tail
            or "install it" in tail
            or "not installed" in tail
        ):
            os.write(fd, b"yes\r")
            sent_yes = True
            sent_yes_at = time.time()
    now = time.time()
    if sent_yes and now - sent_yes_at > 2 and now - last_help_check > 2:
        last_help_check = now
        if copilot_help_ready():
            stop_child(pid)
            try:
                os.close(fd)
            except OSError:
                pass
            sys.exit(0)
    try:
        done_pid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        status = 0
        break
    if done_pid == pid:
        break
else:
    stop_child(pid)
    sys.exit(124)

try:
    os.close(fd)
except OSError:
    pass

if status is None:
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        status = 0

if copilot_help_ready():
    sys.exit(0)

if os.WIFEXITED(status):
    sys.exit(os.WEXITSTATUS(status))
sys.exit(0)
PY
    elif command -v script >/dev/null 2>&1; then
        printf 'yes\n' | timeout 120 script -qfec 'gh copilot' /dev/null || true
    else
        printf 'yes\n' | timeout 120 gh copilot || true
    fi
fi

GH_PROMPT_DISABLED=1 gh copilot -- --help >/dev/null || {
    echo 'Copilot CLI not installed after setup' >&2
    exit 1
}
echo 'copilot command ready'
""".lstrip()


class CommandError(RuntimeError):
    def __init__(self, command: str, returncode: int, stdout: str, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stdout = stdout.strip()
        self.stderr = stderr.strip()
        message = f"{command} failed with exit code {returncode}"
        super().__init__(message)


@dataclass
class RepositoryConfig:
    workflow: str
    gateway_host_port: int
    gateway_user: str
    gateway_secret_user: str
    gateway_secret_port: str
    private_key_path: str
    public_key_path: str


@dataclass
class Workstream:
    name: str
    branch: str
    tunnel_port: int
    max_lifetime: int
    run_id: str = ""
    run_url: str = ""


@dataclass
class WorkflowSession:
    run_id: str
    status: str
    branch: str
    title: str
    url: str
    created_at: str


@dataclass
class ToolModule:
    module_id: str
    label: str
    target: str
    check: str
    install: str
    auth: str


@dataclass
class WidgetConfig:
    repository: RepositoryConfig
    workstreams: list[Workstream]
    tools: list[ToolModule]


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, event: tk.Event[Any]) -> None:
        if self.window is not None:
            return
        window = tk.Toplevel(self.widget)
        window.withdraw()
        window.overrideredirect(True)
        label = tk.Label(
            window,
            text=self.text,
            background=THEME["panel_alt"],
            foreground=THEME["text"],
            borderwidth=1,
            relief="solid",
            padx=8,
            pady=4,
            font=("TkDefaultFont", 9),
        )
        label.pack()
        window.update_idletasks()
        width = window.winfo_width()
        height = window.winfo_height()
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        x_position = max(8, min(event.x_root + 12, screen_width - width - 8))
        y_position = max(8, min(event.y_root + 12, screen_height - height - 8))
        window.geometry(f"+{x_position}+{y_position}")
        window.deiconify()
        self.window = window

    def hide(self, _event: tk.Event[Any] | None = None) -> None:
        if self.window is None:
            return
        self.window.destroy()
        self.window = None


def load_config() -> WidgetConfig:
    source_path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    with source_path.open("r", encoding="utf-8") as config_file:
        raw_config = json.load(config_file)

    repository_raw = raw_config["repository"]
    repository = RepositoryConfig(
        workflow=repository_raw.get("workflow", "debug-runner.yml"),
        gateway_host_port=int(repository_raw.get("gateway_host_port", 50556)),
        gateway_user=repository_raw.get("gateway_user", "gateway"),
        gateway_secret_user=repository_raw.get("gateway_secret_user", "gateway"),
        gateway_secret_port=str(repository_raw.get("gateway_secret_port", "50556")),
        private_key_path=repository_raw.get("private_key_path", "test-assets/id_rsa_test"),
        public_key_path=repository_raw.get("public_key_path", "test-assets/id_rsa_test.pub"),
    )

    workstreams = [
        Workstream(
            name=str(item["name"]),
            branch=str(item.get("branch", "main")),
            tunnel_port=int(item["tunnel_port"]),
            max_lifetime=int(item.get("max_lifetime", 3600)),
        )
        for item in raw_config.get("workstreams", [])
    ]
    if not workstreams:
        workstreams = [Workstream(name="main", branch="main", tunnel_port=2222, max_lifetime=3600)]
    tools = [
        ToolModule(
            module_id=str(item["id"]),
            label=str(item["label"]),
            target=str(item.get("target", "runner")),
            check=str(item.get("check", "")),
            install=str(item.get("install", "")),
            auth=str(item.get("auth", "")),
        )
        for item in raw_config.get("tools", [])
    ]
    return WidgetConfig(repository=repository, workstreams=workstreams, tools=tools)


def config_to_json_data(config: WidgetConfig) -> dict[str, Any]:
    return {
        "repository": {
            "workflow": config.repository.workflow,
            "gateway_host_port": config.repository.gateway_host_port,
            "gateway_user": config.repository.gateway_user,
            "gateway_secret_user": config.repository.gateway_secret_user,
            "gateway_secret_port": config.repository.gateway_secret_port,
            "private_key_path": config.repository.private_key_path,
            "public_key_path": config.repository.public_key_path,
        },
        "workstreams": [
            {
                "name": workstream.name,
                "branch": workstream.branch,
                "tunnel_port": workstream.tunnel_port,
                "max_lifetime": workstream.max_lifetime,
            }
            for workstream in config.workstreams
        ],
        "tools": [
            {
                "id": tool.module_id,
                "label": tool.label,
                "target": tool.target,
                "check": tool.check,
                "install": tool.install,
                "auth": tool.auth,
            }
            for tool in config.tools
        ],
    }


def run_process(
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=REPO_ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise CommandError(" ".join(args), completed.returncode, completed.stdout, completed.stderr)
    return completed


def run_shell(command: str, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise CommandError(command, completed.returncode, completed.stdout, completed.stderr)
    return completed


def public_ip() -> str:
    with urllib.request.urlopen("https://icanhazip.com", timeout=10) as response:
        return response.read().decode("utf-8").strip()


def is_unexpected_tunnel_port_input(error: CommandError) -> bool:
    output = f"{error.stdout}\n{error.stderr}"
    return "Unexpected inputs provided" in output and "TUNNEL_PORT" in output


class RunnerWidget:
    def __init__(self, root: tk.Tk, config: WidgetConfig) -> None:
        self.root = root
        self.config = config
        self.events: queue.Queue[Callable[[], None]] = queue.Queue()
        self.bridge_enabled = tk.BooleanVar(value=False)
        self.selected_workstream = tk.StringVar(value=config.workstreams[0].name if config.workstreams else "")
        self.selected_session = tk.StringVar(value="")
        self.workflow_sessions: dict[str, WorkflowSession] = {}
        self.session_picker: ttk.Combobox | None = None
        self.workstream_enabled: dict[str, tk.BooleanVar] = {}
        self.workstream_status: dict[str, tk.StringVar] = {}
        self.workstream_status_lights: dict[str, tk.Canvas] = {}
        self.tool_enabled: dict[str, tk.BooleanVar] = {}
        self.tool_status: dict[str, tk.StringVar] = {}
        self.tool_status_lights: dict[str, tk.Canvas] = {}
        self.bridge_status = tk.StringVar(value="not checked")
        self.bridge_status_light: tk.Canvas | None = None
        self.shell_process: subprocess.Popen[Any] | None = None
        self.shell_master_fd: int | None = None
        self.embedded_terminal_process: subprocess.Popen[Any] | None = None
        self.embedded_terminal_session_id = 0
        self.embedded_terminal_launch_after_id: str | None = None
        self.embedded_terminal_frame: tk.Frame | None = None
        self.embedded_terminal_workstream: str | None = None
        self.shell_session_id = 0
        self.shell_status = tk.StringVar(value="disconnected")
        self.last_tool_failure: dict[str, str] = {}
        self.log_widgets: list[tk.Text] = []
        self.tooltips: list[ToolTip] = []
        self.shell_menu: tk.Menu | None = None
        self.notebook: ttk.Notebook | None = None
        self.shell_tab: ttk.Frame | None = None
        self.workstreams_rows_frame: ttk.Frame | None = None
        self.workstream_picker: ttk.Combobox | None = None
        self.shell_workstream_picker: ttk.Combobox | None = None
        self.auth_poll_generations: dict[str, int] = {}

        self.root.title("Runner Bridge")
        self.root.geometry("860x620")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.configure_style()
        self.build_layout()
        self.root.after(100, self.drain_events)
        self.root.after(1000, self.refresh_sessions)

    def configure_style(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.root.configure(background=THEME["app_bg"])
        style.configure("TFrame", background=THEME["app_bg"])
        style.configure("Panel.TFrame", background=THEME["panel_bg"], relief="flat")
        style.configure("Title.TLabel", background=THEME["app_bg"], foreground=THEME["text"], font=("TkDefaultFont", 15, "bold"))
        style.configure("Subtitle.TLabel", background=THEME["app_bg"], foreground=THEME["muted"], font=("TkDefaultFont", 10))
        style.configure("Section.TLabel", background=THEME["panel_bg"], foreground=THEME["text"], font=("TkDefaultFont", 11, "bold"))
        style.configure("Muted.TLabel", background=THEME["panel_bg"], foreground=THEME["muted"], font=("TkDefaultFont", 9))
        style.configure("Status.TLabel", background=THEME["panel_bg"], foreground=THEME["status"], font=("TkDefaultFont", 9, "bold"))
        style.configure("TButton", padding=(5, 2))
        style.configure("Primary.TButton", padding=(6, 3))
        style.configure("Danger.TButton", foreground=THEME["danger_line"])
        style.configure("TNotebook", background=THEME["app_bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=THEME["panel_alt"], foreground=THEME["muted"], padding=(10, 4))
        style.map(
            "TNotebook.Tab",
            background=[("selected", THEME["panel_bg"])],
            foreground=[("selected", THEME["text"])],
        )
        style.configure("TCheckbutton", background=THEME["panel_bg"], foreground=THEME["text"])
        style.map("TCheckbutton", background=[("active", THEME["panel_bg"])], foreground=[("active", THEME["text"])])
        style.configure("TMenubutton", background=THEME["panel_alt"], foreground=THEME["text"])

    def build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Runner Bridge", style="Title.TLabel").pack(side="left")

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        self.notebook = notebook

        controls_tab = ttk.Frame(notebook, padding=8)
        shell_tab = ttk.Frame(notebook, padding=0)
        self.shell_tab = shell_tab
        notebook.add(controls_tab, text="Controls")
        notebook.add(shell_tab, text="Shell")

        controls_tab.rowconfigure(0, weight=1)
        controls_tab.columnconfigure(0, weight=1)

        split_pane = ttk.PanedWindow(controls_tab, orient="horizontal")
        split_pane.grid(row=0, column=0, sticky="nsew")
        control_stack = ttk.Frame(split_pane)
        activity_rail = ttk.Frame(split_pane, style="Panel.TFrame", padding=8)
        activity_rail.rowconfigure(1, weight=1)
        activity_rail.columnconfigure(0, weight=1)
        split_pane.add(control_stack, weight=3)
        split_pane.add(activity_rail, weight=2)

        self.build_bridge_panel(control_stack)
        self.build_workstreams_panel(control_stack)
        self.build_tools_panel(control_stack)
        self.build_log_panel(activity_rail, compact=True)
        self.build_shell_panel(shell_tab)
        self.log("Ready. Config loaded from config.json or config.example.json.")

    def workstream_names(self) -> list[str]:
        return [workstream.name for workstream in self.config.workstreams]

    def update_workstream_pickers(self) -> None:
        names = self.workstream_names()
        if self.selected_workstream.get() not in names:
            self.selected_workstream.set(names[0] if names else "")
        for picker in (self.workstream_picker, self.shell_workstream_picker):
            if picker is not None:
                picker.configure(values=names)

    def save_local_config(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = config_to_json_data(self.config)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CONFIG_PATH.parent, delete=False) as config_file:
                json.dump(data, config_file, indent=2)
                config_file.write("\n")
                temp_path = Path(config_file.name)
            os.replace(temp_path, CONFIG_PATH)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def panel(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=8)
        frame.pack(fill="x", pady=(0, 6))
        return frame

    def icon_button(
        self,
        parent: ttk.Frame,
        icon: str,
        command: Callable[[], None],
        tooltip: str,
        tone: str = "neutral",
    ) -> tk.Canvas:
        colors = {
            "neutral": (THEME["neutral_fill"], THEME["neutral_line"]),
            "primary": (THEME["primary_fill"], THEME["primary_line"]),
            "danger": (THEME["danger_fill"], THEME["danger_line"]),
        }
        fill, foreground = colors.get(tone, colors["neutral"])
        size = 26
        canvas = tk.Canvas(
            parent,
            width=size,
            height=size,
            highlightthickness=0,
            borderwidth=0,
            background=THEME["panel_bg"],
            cursor="hand2",
            takefocus=1,
        )
        canvas.create_oval(2, 2, size - 2, size - 2, fill=fill, outline=foreground, width=1)
        self.draw_icon(canvas, icon, foreground)
        canvas.bind("<Button-1>", lambda _event: command())
        canvas.bind("<Return>", lambda _event: command())
        canvas.bind("<space>", lambda _event: command())
        self.tooltips.append(ToolTip(canvas, tooltip))
        return canvas

    def draw_icon(self, canvas: tk.Canvas, icon: str, color: str) -> None:
        if icon == "local":
            canvas.create_rectangle(8, 8, 18, 15, outline=color, width=1.5)
            canvas.create_line(11, 18, 15, 18, fill=color, width=1.5)
            canvas.create_line(13, 15, 13, 18, fill=color, width=1.5)
        elif icon == "public":
            canvas.create_oval(7, 7, 19, 19, outline=color, width=1.5)
            canvas.create_line(8, 13, 18, 13, fill=color, width=1.5)
            canvas.create_arc(9, 7, 17, 19, start=80, extent=200, outline=color, width=1.2)
            canvas.create_arc(9, 7, 17, 19, start=260, extent=200, outline=color, width=1.2)
        elif icon == "probe":
            canvas.create_oval(7, 7, 15, 15, outline=color, width=1.5)
            canvas.create_line(14, 14, 19, 19, fill=color, width=1.8)
        elif icon == "copy":
            canvas.create_rectangle(8, 9, 15, 17, outline=color, width=1.4)
            canvas.create_rectangle(11, 7, 18, 15, outline=color, width=1.4)
        elif icon == "setup":
            canvas.create_line(13, 7, 13, 19, fill=color, width=1.8)
            canvas.create_line(7, 13, 19, 13, fill=color, width=1.8)
        elif icon == "fix":
            canvas.create_line(8, 18, 17, 9, fill=color, width=1.8)
            canvas.create_arc(14, 6, 22, 14, start=120, extent=210, outline=color, width=1.5)
            canvas.create_oval(6, 17, 10, 21, outline=color, width=1.4)
        elif icon == "stop":
            canvas.create_line(9, 9, 17, 17, fill=color, width=1.8)
            canvas.create_line(17, 9, 9, 17, fill=color, width=1.8)
        elif icon == "connect":
            canvas.create_rectangle(8, 10, 15, 16, outline=color, width=1.5)
            canvas.create_line(15, 13, 20, 13, fill=color, width=1.8)
            canvas.create_line(18, 10, 18, 16, fill=color, width=1.3)
        elif icon == "terminal":
            canvas.create_rectangle(7, 8, 19, 18, outline=color, width=1.4)
            canvas.create_line(9, 11, 12, 13, fill=color, width=1.4)
            canvas.create_line(9, 15, 14, 15, fill=color, width=1.4)
        elif icon == "send":
            canvas.create_polygon(7, 8, 20, 13, 7, 18, 10, 13, fill="", outline=color, width=1.5)
        elif icon == "clear":
            canvas.create_rectangle(8, 9, 18, 18, outline=color, width=1.4)
            canvas.create_line(10, 11, 16, 17, fill=color, width=1.5)
        elif icon == "interrupt":
            canvas.create_line(13, 7, 13, 14, fill=color, width=1.8)
            canvas.create_oval(11, 17, 15, 21, fill=color, outline=color)
        elif icon == "reconnect":
            canvas.create_arc(7, 7, 19, 19, start=35, extent=285, outline=color, width=1.5)
            canvas.create_line(18, 7, 18, 12, fill=color, width=1.5)
            canvas.create_line(18, 7, 13, 8, fill=color, width=1.5)
        else:
            canvas.create_oval(11, 11, 15, 15, fill=color, outline=color)

    def status_light(self, parent: ttk.Frame) -> tk.Canvas:
        canvas = tk.Canvas(parent, width=14, height=14, highlightthickness=0, borderwidth=0, background=THEME["panel_bg"])
        canvas.create_oval(2, 2, 12, 12, fill="#f59e0b", outline="#f59e0b", tags=("light",))
        return canvas

    def status_color(self, message: str) -> str:
        text = message.lower()
        if any(marker in text for marker in ("failed", "needs fix", "outside", "unavailable", "no captured", "stopped")):
            return "#dc2626"
        if text.startswith("error") or " error:" in text:
            return "#dc2626"
        if re.search(r"\bok\b", text):
            return "#16a34a"
        if text == "connected" or text.startswith("connected:"):
            return "#16a34a"
        if any(marker in text for marker in ("ready", "running", "github ready", "setup done", "nothing to setup")):
            return "#16a34a"
        return "#f59e0b"

    def update_status_light(self, canvas: tk.Canvas | None, message: str) -> None:
        if canvas is None:
            return
        color = self.status_color(message)
        canvas.itemconfigure("light", fill=color, outline=color)

    def compact_status(self, message: str, limit: int = 18) -> str:
        one_line = " ".join(message.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1] + "~"

    def set_bridge_status(self, message: str) -> None:
        self.bridge_status.set(self.compact_status(message, 16))
        self.update_status_light(self.bridge_status_light, message)

    def set_workstream_status(self, workstream_name: str, message: str) -> None:
        self.workstream_status[workstream_name].set(self.compact_status(message, 16))
        self.update_status_light(self.workstream_status_lights.get(workstream_name), message)

    def set_tool_status(self, module_id: str, message: str) -> None:
        self.tool_status[module_id].set(self.compact_status(message, 16))
        self.update_status_light(self.tool_status_lights.get(module_id), message)

    def build_bridge_panel(self, parent: ttk.Frame) -> None:
        panel = self.panel(parent)
        row = ttk.Frame(panel, style="Panel.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="1. Bridge", style="Section.TLabel").pack(side="left")
        self.bridge_status_light = self.status_light(row)
        self.bridge_status_light.pack(side="left", padx=(8, 4))
        ttk.Label(row, textvariable=self.bridge_status, style="Status.TLabel", width=16).pack(side="left")

        controls = ttk.Frame(panel, style="Panel.TFrame")
        controls.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(
            controls,
            text="On",
            variable=self.bridge_enabled,
            command=self.toggle_bridge,
        ).pack(side="left")
        self.icon_button(controls, "local", self.check_local_gateway, "Check local gateway SSH", "primary").pack(side="left", padx=(8, 2))
        self.icon_button(controls, "public", self.check_public_gateway, "Check public gateway SSH", "primary").pack(side="left", padx=2)
        self.icon_button(controls, "stop", self.stop_bridge, "Stop Docker gateway", "danger").pack(side="right")
        self.set_bridge_status("not checked")

    def build_workstreams_panel(self, parent: ttk.Frame) -> None:
        panel = self.panel(parent)
        top_row = ttk.Frame(panel, style="Panel.TFrame")
        top_row.pack(fill="x")
        ttk.Label(top_row, text="2. Runners", style="Section.TLabel").pack(side="left")
        ttk.Button(top_row, text="Add Runner", command=self.add_workstream).pack(side="right")
        ttk.Label(top_row, text="tool target", style="Muted.TLabel").pack(side="right", padx=(8, 6))
        self.workstream_picker = ttk.Combobox(
            top_row,
            textvariable=self.selected_workstream,
            values=self.workstream_names(),
            state="readonly",
            width=18,
        )
        self.workstream_picker.pack(side="right")

        self.workstreams_rows_frame = ttk.Frame(panel, style="Panel.TFrame")
        self.workstreams_rows_frame.pack(fill="x")
        self.build_workstream_rows()

        session_row = ttk.Frame(panel, style="Panel.TFrame")
        session_row.pack(fill="x", pady=(8, 0))
        ttk.Label(session_row, text="sessions", style="Muted.TLabel", width=9).pack(side="left")
        self.session_picker = ttk.Combobox(session_row, textvariable=self.selected_session, values=(), state="readonly", width=38)
        self.session_picker.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.icon_button(session_row, "reconnect", self.refresh_sessions, "Refresh running workflow sessions", "primary").pack(side="left", padx=2)
        self.icon_button(session_row, "terminal", self.resume_selected_session, "Resume selected session using the selected workstream port").pack(side="left", padx=2)
        self.icon_button(session_row, "stop", self.cancel_selected_session, "Cancel selected workflow session", "danger").pack(side="left", padx=2)
        self.update_workstream_pickers()

    def build_workstream_rows(self) -> None:
        if self.workstreams_rows_frame is None:
            return
        for child in self.workstreams_rows_frame.winfo_children():
            child.destroy()
        for workstream in self.config.workstreams:
            if workstream.name not in self.workstream_enabled:
                self.workstream_enabled[workstream.name] = tk.BooleanVar(value=False)
            if workstream.name not in self.workstream_status:
                self.workstream_status[workstream.name] = tk.StringVar(value="idle")
            row = ttk.Frame(self.workstreams_rows_frame, style="Panel.TFrame")
            row.pack(fill="x", pady=(5, 0))
            self.workstream_status_lights[workstream.name] = self.status_light(row)
            self.workstream_status_lights[workstream.name].pack(side="left", padx=(0, 5))
            ttk.Label(row, text=f"{workstream.name}:{workstream.tunnel_port}", style="Muted.TLabel", width=17).pack(side="left")
            ttk.Label(row, textvariable=self.workstream_status[workstream.name], style="Status.TLabel", width=16).pack(side="left")
            self.icon_button(
                row,
                "probe",
                lambda item=workstream: self.probe_or_start_workstream(item),
                f"Probe or start {workstream.name} runner",
                "primary",
            ).pack(side="left", padx=(4, 2))
            self.icon_button(
                row,
                "copy",
                lambda item=workstream: self.copy_ssh_command(item),
                f"Copy SSH command for {workstream.name}",
            ).pack(side="left", padx=2)
            self.icon_button(
                row,
                "stop",
                lambda item=workstream: self.cancel_workstream(item),
                f"Cancel {workstream.name} workflow run",
                "danger",
            ).pack(side="left", padx=2)
            self.update_status_light(self.workstream_status_lights[workstream.name], self.workstream_status[workstream.name].get())

    def sanitize_workstream_name(self, raw_name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw_name.strip().lower())
        cleaned = re.sub(r"-+", "-", cleaned).strip("-_")
        return cleaned

    def next_tunnel_port(self) -> int | None:
        used_ports = {workstream.tunnel_port for workstream in self.config.workstreams}
        for port in range(TUNNEL_PORT_MIN, TUNNEL_PORT_MAX + 1):
            if port not in used_ports:
                return port
        return None

    def add_workstream(self) -> None:
        raw_name = simpledialog.askstring("Add Runner", "Runner name", parent=self.root)
        if raw_name is None:
            return
        name = self.sanitize_workstream_name(raw_name)
        if not name:
            messagebox.showwarning("Add Runner", "Enter a runner name using letters, numbers, dashes, or underscores.", parent=self.root)
            return
        existing_names = {workstream.name.lower() for workstream in self.config.workstreams}
        if name.lower() in existing_names:
            messagebox.showwarning("Add Runner", f"Runner '{name}' already exists.", parent=self.root)
            return
        port = self.next_tunnel_port()
        if port is None:
            messagebox.showwarning("Add Runner", f"No free tunnel ports remain in {TUNNEL_PORT_MIN}-{TUNNEL_PORT_MAX}.", parent=self.root)
            return
        workstream = Workstream(name=name, branch="main", tunnel_port=port, max_lifetime=3600)
        self.config.workstreams.append(workstream)
        self.selected_workstream.set(name)
        self.workstream_enabled[name] = tk.BooleanVar(value=False)
        self.workstream_status[name] = tk.StringVar(value="idle")
        self.build_workstream_rows()
        self.update_workstream_pickers()
        try:
            self.save_local_config()
        except OSError as error:
            messagebox.showerror("Add Runner", f"Runner added for this session, but config.json could not be saved: {error}", parent=self.root)
        self.log(f"Added runner {name} on port {port}; starting it now. Port was auto-assigned from local config.")
        self.start_workstream(workstream)

    def build_tools_panel(self, parent: ttk.Frame) -> None:
        panel = self.panel(parent)
        ttk.Label(panel, text="3. Tools", style="Section.TLabel").pack(anchor="w")

        for tool in self.config.tools:
            self.tool_enabled[tool.module_id] = tk.BooleanVar(value=True)
            self.tool_status[tool.module_id] = tk.StringVar(value="unknown")
            row = ttk.Frame(panel, style="Panel.TFrame")
            row.pack(fill="x", pady=(5, 0))
            self.tool_status_lights[tool.module_id] = self.status_light(row)
            self.tool_status_lights[tool.module_id].pack(side="left", padx=(0, 5))
            ttk.Label(row, text=tool.label, style="Muted.TLabel", width=18).pack(side="left")
            ttk.Label(row, textvariable=self.tool_status[tool.module_id], style="Status.TLabel", width=16).pack(side="left")
            self.icon_button(row, "probe", lambda item=tool: self.check_tool(item), f"Check {tool.label}", "primary").pack(side="left", padx=(4, 2))
            self.icon_button(row, "setup", lambda item=tool: self.install_tool(item), f"Set up {tool.label}").pack(side="left", padx=2)
            if tool.module_id != "copilot":
                self.icon_button(row, "fix", lambda item=tool: self.copy_auth_command(item), f"Fix or authenticate {tool.label}", "danger").pack(side="left", padx=2)
            self.set_tool_status(tool.module_id, "unknown")

    def build_shell_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=6)
        panel.pack(fill="both", expand=True)
        panel.rowconfigure(1, weight=1, minsize=380)
        panel.columnconfigure(0, weight=1)

        top = ttk.Frame(panel, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(top, text="Runner Shell", style="Section.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.shell_status, style="Status.TLabel").pack(side="left", padx=(10, 0))
        if self.config.workstreams:
            self.shell_workstream_picker = ttk.Combobox(
                top,
                textvariable=self.selected_workstream,
                values=self.workstream_names(),
                state="readonly",
                width=18,
            )
            self.shell_workstream_picker.pack(side="right")
            ttk.Label(top, text="target", style="Muted.TLabel").pack(side="right", padx=(0, 6))

        self.embedded_terminal_frame = tk.Frame(
            panel,
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            background=THEME["activity_bg"],
        )
        self.embedded_terminal_frame.grid(row=1, column=0, sticky="nsew")
        self.embedded_terminal_frame.grid_remove()

        self.shell_text = tk.Text(
            panel,
            height=18,
            wrap="char",
            borderwidth=0,
            background=THEME["activity_bg"],
            foreground=THEME["activity_fg"],
            insertbackground=THEME["activity_fg"],
        )
        self.shell_text.grid(row=1, column=0, sticky="nsew")
        self.build_shell_context_menu()
        self.shell_text.bind("<Button-3>", self.show_shell_menu)
        self.shell_text.bind("<Button-2>", self.show_shell_menu)
        self.shell_text.bind("<Control-Button-1>", self.show_shell_menu)
        self.shell_text.bind("<Button-1>", lambda _event: self.shell_text.focus_set(), add="+")
        self.shell_text.bind("<Control-c>", lambda _event: self.interrupt_shell() or "break")
        self.shell_text.bind("<Control-C>", lambda _event: self.interrupt_shell() or "break")
        self.shell_text.bind("<Control-Shift-C>", lambda _event: self.copy_shell_selection() or "break")
        self.shell_text.bind("<Control-v>", lambda _event: self.paste_shell_clipboard() or "break")
        self.shell_text.bind("<Control-V>", lambda _event: self.paste_shell_clipboard() or "break")
        self.shell_text.bind("<Control-Shift-V>", lambda _event: self.paste_shell_clipboard() or "break")
        self.shell_text.bind("<KeyPress>", self.handle_shell_key)

        bottom = ttk.Frame(panel, style="Panel.TFrame")
        bottom.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, text="Docked terminal with external fallback", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.icon_button(bottom, "terminal", self.open_embedded_terminal_shell, "Dock runner SSH inside the app", "primary").grid(row=0, column=1, padx=2)
        self.icon_button(bottom, "connect", self.open_system_terminal_shell, "Open runner SSH in a local terminal window").grid(row=0, column=2, padx=2)
        self.icon_button(bottom, "stop", self.disconnect_embedded_terminal, "Close docked terminal", "danger").grid(row=0, column=3, padx=2)

        self.append_shell("Choose a runner, then dock a terminal inside the app for a full interactive shell.\n")

    def build_shell_context_menu(self) -> None:
        self.shell_menu = tk.Menu(
            self.root,
            tearoff=0,
            background=THEME["panel_alt"],
            foreground=THEME["text"],
            activebackground=THEME["neutral_fill"],
            activeforeground=THEME["text"],
            borderwidth=1,
        )
        self.shell_menu.add_command(label="Copy", command=self.copy_shell_selection)
        self.shell_menu.add_command(label="Paste", command=self.paste_shell_clipboard)
        self.shell_menu.add_command(label="Select All", command=self.select_shell_all)
        self.shell_menu.add_separator()
        self.shell_menu.add_command(label="Dock Terminal", command=self.open_embedded_terminal_shell)
        self.shell_menu.add_command(label="Open Local Terminal", command=self.open_system_terminal_shell)

    def show_shell_menu(self, event: tk.Event[Any]) -> str:
        if self.shell_menu is None:
            return "break"
        has_selection = bool(self.shell_text.tag_ranges("sel"))
        self.shell_menu.entryconfigure(0, state="normal" if has_selection else "disabled")
        try:
            clipboard_text = self.root.clipboard_get()
        except tk.TclError:
            clipboard_text = ""
        self.shell_menu.entryconfigure(1, state="normal" if clipboard_text and self.shell_is_connected() else "disabled")
        try:
            self.shell_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.shell_menu.grab_release()
        return "break"

    def copy_shell_selection(self) -> None:
        try:
            selected_text = self.shell_text.get("sel.first", "sel.last")
        except tk.TclError:
            self.root.bell()
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(selected_text)

    def select_shell_all(self) -> None:
        self.shell_text.tag_add("sel", "1.0", "end-1c")
        self.shell_text.mark_set("insert", "end-1c")
        self.shell_text.see("insert")

    def paste_shell_clipboard(self) -> None:
        try:
            clipboard_text = self.root.clipboard_get()
        except tk.TclError:
            self.root.bell()
            return
        normalized_text = clipboard_text.replace("\r\n", "\n").replace("\r", "\n")
        self.write_shell_input(normalized_text.encode("utf-8"))

    def handle_shell_key(self, event: tk.Event[Any]) -> str:
        sequences = {
            "Return": b"\r",
            "KP_Enter": b"\r",
            "BackSpace": b"\x7f",
            "Tab": b"\t",
            "Escape": b"\x1b",
            "Up": b"\x1b[A",
            "Down": b"\x1b[B",
            "Right": b"\x1b[C",
            "Left": b"\x1b[D",
            "Home": b"\x1b[H",
            "End": b"\x1b[F",
            "Delete": b"\x1b[3~",
            "Prior": b"\x1b[5~",
            "Next": b"\x1b[6~",
        }
        data = sequences.get(event.keysym)
        if data is None and event.char:
            data = event.char.encode("utf-8")
        if data:
            self.shell_text.tag_remove("sel", "1.0", "end")
            self.write_shell_input(data)
        return "break"

    def clear_shell_display(self) -> None:
        self.shell_text.delete("1.0", "end")
        if self.shell_is_connected():
            self.append_shell("Shell display cleared. Session is still running; use Interrupt or Reconnect if a prompt is stuck.\n")
        else:
            self.append_shell("Shell display cleared.\n")

    def build_log_panel(self, parent: ttk.Frame, compact: bool = False) -> None:
        panel = parent if compact else ttk.Frame(parent, style="Panel.TFrame", padding=8)
        if not compact:
            panel.pack(fill="both", expand=True)
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)
        ttk.Label(panel, text="Activity", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        log_text = tk.Text(
            panel,
            height=12 if compact else 18,
            width=28 if compact else 80,
            wrap="word",
            borderwidth=0,
            background=THEME["activity_bg"],
            foreground=THEME["activity_fg"],
            insertbackground=THEME["activity_fg"],
        )
        log_text.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.log_widgets.append(log_text)

    def drain_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            event()
        self.root.after(100, self.drain_events)

    def enqueue(self, event: Callable[[], None]) -> None:
        self.events.put(event)

    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}\n"
        for log_widget in self.log_widgets:
            log_widget.insert("end", entry)
            log_widget.see("end")

    def append_shell(self, message: str) -> None:
        clean_message = ANSI_ESCAPE_RE.sub("", message).replace("\r\n", "\n").replace("\r", "\n")
        index = 0
        while index < len(clean_message):
            character = clean_message[index]
            if character == "\a":
                index += 1
                continue
            if character == "\b":
                if clean_message[index : index + 3] == "\b \b" and self.shell_text.compare("end-1c", ">", "1.0"):
                    self.shell_text.delete("end-2c", "end-1c")
                    index += 3
                    continue
                index += 1
                continue
            self.shell_text.insert("end", character)
            index += 1
        self.shell_text.mark_set("insert", "end-1c")
        self.shell_text.see("end")

    def on_close(self) -> None:
        self.disconnect_embedded_terminal(log_message=False)
        self.disconnect_shell(log_message=False)
        self.root.destroy()

    def run_background(
        self,
        label: str,
        action: Callable[[], str | None],
        on_done: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        def worker() -> None:
            try:
                result = action() or "done"
            except CommandError as error:
                detail = error.stderr or error.stdout or str(error)
                self.enqueue(lambda: self.log(f"{label} failed: {detail}"))
                if on_error:
                    self.enqueue(lambda: on_error(detail))
                return
            except Exception as error:  # noqa: BLE001 - UI boundary should show unexpected failures.
                detail = str(error)
                self.enqueue(lambda: self.log(f"{label} failed: {detail}"))
                if on_error:
                    self.enqueue(lambda: on_error(detail))
                return
            self.enqueue(lambda: self.log(f"{label}: {result}"))
            if on_done:
                self.enqueue(lambda: on_done(result))

        self.log(f"{label} started")
        threading.Thread(target=worker, daemon=True).start()

    def private_key_path(self) -> Path:
        return REPO_ROOT / self.config.repository.private_key_path

    def public_key_path(self) -> Path:
        return REPO_ROOT / self.config.repository.public_key_path

    def ssh_args(self, port: int, user: str = "runner") -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.private_key_path()),
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "LogLevel=ERROR",
            f"{user}@localhost",
        ]

    def interactive_ssh_args(self, workstream: Workstream) -> list[str]:
        return [
            "ssh",
            "-tt",
            "-i",
            str(self.private_key_path()),
            "-p",
            str(workstream.tunnel_port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "LogLevel=ERROR",
            "runner@localhost",
        ]

    def interactive_ssh_command(self, workstream: Workstream, remote_command: str | None = None) -> str:
        args = self.interactive_ssh_args(workstream)
        if remote_command:
            args.append(remote_command)
        return shlex.join(args)

    def embedded_terminal_available(self) -> bool:
        return sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY")) and shutil.which("xterm") is not None

    def cancel_pending_embedded_terminal_launch(self) -> None:
        if self.embedded_terminal_launch_after_id is None:
            return
        try:
            self.root.after_cancel(self.embedded_terminal_launch_after_id)
        except tk.TclError:
            pass
        self.embedded_terminal_launch_after_id = None

    def show_shell_text(self) -> None:
        if self.embedded_terminal_frame is not None:
            self.embedded_terminal_frame.grid_remove()
        if hasattr(self, "shell_text"):
            self.shell_text.grid(row=1, column=0, sticky="nsew")

    def show_embedded_terminal_frame(self) -> None:
        if hasattr(self, "shell_text"):
            self.shell_text.grid_remove()
        if self.embedded_terminal_frame is not None:
            self.embedded_terminal_frame.grid(row=1, column=0, sticky="nsew")

    def terminal_script(self, ssh_command: str) -> str:
        return (
            f"cd {shlex.quote(str(REPO_ROOT))}\n"
            f"{ssh_command}\n"
            "status=$?\n"
            "printf '\\nSSH session ended with exit code %s. Press Enter to close...' \"$status\"\n"
            "read _\n"
        )

    def terminal_launcher_args(self, ssh_command: str) -> list[str] | None:
        script = self.terminal_script(ssh_command)
        if sys.platform == "darwin" and shutil.which("osascript"):
            return [
                "osascript",
                "-e",
                f"tell application \"Terminal\" to do script {json.dumps(script)}",
                "-e",
                "tell application \"Terminal\" to activate",
            ]

        shell = os.environ.get("SHELL") or "/bin/sh"
        terminal_env = os.environ.get("TERMINAL")
        if terminal_env:
            terminal_parts = shlex.split(terminal_env)
            if terminal_parts and shutil.which(terminal_parts[0]):
                return terminal_parts + ["-e", shell, "-lc", script]

        candidates: list[tuple[str, list[str]]] = [
            ("x-terminal-emulator", ["x-terminal-emulator", "-e", shell, "-lc", script]),
            ("gnome-terminal", ["gnome-terminal", "--", shell, "-lc", script]),
            ("kgx", ["kgx", "--", shell, "-lc", script]),
            ("konsole", ["konsole", "-e", shell, "-lc", script]),
            ("xfce4-terminal", ["xfce4-terminal", "-e", f"{shlex.quote(shell)} -lc {shlex.quote(script)}"]),
            ("mate-terminal", ["mate-terminal", "--", shell, "-lc", script]),
            ("tilix", ["tilix", "-e", shell, "-lc", script]),
            ("wezterm", ["wezterm", "start", "--", shell, "-lc", script]),
            ("kitty", ["kitty", shell, "-lc", script]),
            ("alacritty", ["alacritty", "-e", shell, "-lc", script]),
            ("xterm", ["xterm", "-e", shell, "-lc", script]),
        ]
        for executable, args in candidates:
            if shutil.which(executable):
                return args
        return None

    def embedded_terminal_geometry(self) -> str:
        if self.embedded_terminal_frame is None:
            return "80x24+0+0"
        width = self.embedded_terminal_frame.winfo_width()
        height = self.embedded_terminal_frame.winfo_height()
        if width <= 1:
            width = 820
        if height <= 1:
            height = 420
        try:
            font = tkfont.Font(root=self.root, family="Monospace", size=10)
            cell_width = max(6, font.measure("M"))
            cell_height = max(12, font.metrics("linespace"))
        except tk.TclError:
            cell_width = 8
            cell_height = 18
        columns = max(72, min(160, max(1, (width - 12) // cell_width)))
        rows = max(8, min(48, max(1, (height - 16) // cell_height)))
        return f"{columns}x{rows}+0+0"

    def embedded_terminal_args(self, ssh_command: str, window_id: int, geometry: str | None = None) -> list[str]:
        script = self.terminal_script(ssh_command)
        shell = os.environ.get("SHELL") or "/bin/sh"
        return [
            "xterm",
            "-into",
            str(window_id),
            "-title",
            "Runner Shell",
            "-fa",
            "Monospace",
            "-fs",
            "10",
            "-b",
            "0",
            "-bw",
            "0",
            "-bg",
            THEME["activity_bg"],
            "-fg",
            THEME["activity_fg"],
            "-xrm",
            "*scrollBar: false",
            "-xrm",
            "*internalBorder: 0",
            "-xrm",
            "*borderWidth: 0",
            "-xrm",
            "*selectToClipboard: true",
            "-xrm",
            "*VT100.Translations: #override\nCtrl Shift <Key>C: copy-selection(CLIPBOARD)\nCtrl Shift <Key>V: insert-selection(CLIPBOARD)\nShift <Key>Insert: insert-selection(CLIPBOARD)",
            "-geometry",
            geometry or "80x24+0+0",
            "-e",
            shell,
            "-lc",
            script,
        ]

    def run_on_runner(
        self,
        workstream: Workstream,
        command: str,
        timeout: int = 120,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return run_process(self.ssh_args(workstream.tunnel_port) + [command], input_text=input_text, timeout=timeout)

    def selected_workstream_item(self) -> Workstream:
        selected_name = self.selected_workstream.get()
        for workstream in self.config.workstreams:
            if workstream.name == selected_name:
                return workstream
        if not self.config.workstreams:
            raise RuntimeError("No workstreams are configured")
        return self.config.workstreams[0]

    def shell_is_connected(self) -> bool:
        return self.shell_process is not None and self.shell_process.poll() is None and self.shell_master_fd is not None

    def connect_shell(self) -> None:
        if self.notebook is not None and self.shell_tab is not None:
            self.notebook.select(self.shell_tab)
        if self.shell_is_connected():
            self.shell_status.set("connected")
            self.shell_text.focus_set()
            return
        workstream = self.selected_workstream_item()
        try:
            master_fd, slave_fd = pty.openpty()
            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")
            process = subprocess.Popen(
                self.interactive_ssh_args(workstream),
                cwd=REPO_ROOT,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                env=env,
            )
            os.close(slave_fd)
        except Exception as error:  # noqa: BLE001 - GUI boundary should show connection failures.
            self.append_shell(f"Shell failed to connect: {error}\n")
            self.shell_status.set("failed")
            return

        self.shell_session_id += 1
        session_id = self.shell_session_id
        self.shell_master_fd = master_fd
        self.shell_process = process
        self.shell_status.set(f"connected:{workstream.name}")
        self.append_shell(f"Connecting to {workstream.name} on port {workstream.tunnel_port}...\n")
        self.shell_text.focus_set()
        threading.Thread(target=self.read_shell_output, args=(session_id, master_fd, process), daemon=True).start()

    def embedded_terminal_is_running(self) -> bool:
        return self.embedded_terminal_process is not None and self.embedded_terminal_process.poll() is None

    def open_embedded_terminal_shell(
        self,
        command_to_copy: str | None = None,
        remote_command: str | None = None,
    ) -> None:
        if self.notebook is not None and self.shell_tab is not None:
            self.notebook.select(self.shell_tab)
        if command_to_copy:
            self.root.clipboard_clear()
            self.root.clipboard_append(command_to_copy)
        workstream = self.selected_workstream_item()
        if self.embedded_terminal_is_running():
            if remote_command:
                clipboard_command = command_to_copy or remote_command
                self.root.clipboard_clear()
                self.root.clipboard_append(clipboard_command)
                active_name = self.embedded_terminal_workstream or "active"
                self.shell_status.set(f"docked:{active_name}")
                self.log(f"Copied {clipboard_command} for the active docked terminal")
                return
            if command_to_copy:
                active_name = self.embedded_terminal_workstream or "active"
                self.shell_status.set(f"docked:{active_name}")
                self.log("Command copied to clipboard for the active docked terminal")
                return
            if self.embedded_terminal_workstream == workstream.name:
                self.shell_status.set(f"docked:{workstream.name}")
                return
        if not self.embedded_terminal_available() or self.embedded_terminal_frame is None:
            self.append_shell("Docked terminal is unavailable here; opening a local terminal window instead.\n")
            self.open_system_terminal_shell(command_to_copy=command_to_copy)
            return

        self.disconnect_shell(log_message=False)
        self.cancel_pending_embedded_terminal_launch()
        if self.embedded_terminal_is_running():
            self.disconnect_embedded_terminal(log_message=False)

        ssh_command = self.interactive_ssh_command(workstream, remote_command)
        self.show_embedded_terminal_frame()
        self.shell_status.set(f"docking:{workstream.name}")
        self.embedded_terminal_launch_after_id = self.root.after(
            75,
            lambda: self.launch_embedded_terminal_process(workstream, ssh_command, command_to_copy),
        )

    def launch_embedded_terminal_process(
        self,
        workstream: Workstream,
        ssh_command: str,
        command_to_copy: str | None = None,
    ) -> None:
        self.embedded_terminal_launch_after_id = None
        if self.embedded_terminal_frame is None:
            self.show_shell_text()
            self.open_system_terminal_shell(command_to_copy=command_to_copy)
            return
        self.root.update_idletasks()
        self.embedded_terminal_frame.update_idletasks()
        window_id = self.embedded_terminal_frame.winfo_id()
        geometry = self.embedded_terminal_geometry()
        try:
            process = subprocess.Popen(
                self.embedded_terminal_args(ssh_command, window_id, geometry),
                cwd=REPO_ROOT,
                start_new_session=True,
            )
        except Exception as error:  # noqa: BLE001 - GUI boundary should show launch failures.
            self.show_shell_text()
            self.shell_status.set("dock failed")
            self.append_shell(f"Could not dock xterm: {error}\n")
            self.open_system_terminal_shell(command_to_copy=command_to_copy)
            return

        self.embedded_terminal_session_id += 1
        session_id = self.embedded_terminal_session_id
        self.embedded_terminal_process = process
        self.embedded_terminal_workstream = workstream.name
        self.shell_status.set(f"docked:{workstream.name}")
        self.append_shell(f"Docked terminal for {workstream.name} on port {workstream.tunnel_port}.\n")
        if command_to_copy:
            self.append_shell("Command copied to clipboard. Paste it in the docked terminal when the runner prompt is ready.\n")
        self.log(f"Docked terminal for {workstream.name}")
        threading.Thread(target=self.wait_for_embedded_terminal, args=(session_id, process), daemon=True).start()

    def wait_for_embedded_terminal(self, session_id: int, process: subprocess.Popen[Any]) -> None:
        process.wait()
        self.enqueue(lambda session_id=session_id: self.mark_embedded_terminal_disconnected(session_id))

    def mark_embedded_terminal_disconnected(self, session_id: int) -> None:
        if session_id != self.embedded_terminal_session_id:
            return
        self.embedded_terminal_process = None
        self.embedded_terminal_workstream = None
        self.show_shell_text()
        if not self.shell_is_connected():
            self.shell_status.set("disconnected")

    def disconnect_embedded_terminal(self, log_message: bool = True) -> None:
        self.cancel_pending_embedded_terminal_launch()
        process = self.embedded_terminal_process
        self.embedded_terminal_session_id += 1
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self.embedded_terminal_process = None
        self.embedded_terminal_workstream = None
        self.show_shell_text()
        if not self.shell_is_connected():
            self.shell_status.set("disconnected")
        if log_message and hasattr(self, "shell_text"):
            self.append_shell("Docked terminal closed.\n")

    def open_system_terminal_shell(self, command_to_copy: str | None = None, remote_command: str | None = None) -> None:
        if self.notebook is not None and self.shell_tab is not None:
            self.notebook.select(self.shell_tab)
        workstream = self.selected_workstream_item()
        ssh_command = self.interactive_ssh_command(workstream, remote_command)
        launcher_args = self.terminal_launcher_args(ssh_command)
        if command_to_copy:
            self.root.clipboard_clear()
            self.root.clipboard_append(command_to_copy)

        if launcher_args is None:
            if not command_to_copy:
                self.root.clipboard_clear()
                self.root.clipboard_append(ssh_command)
            self.shell_status.set("terminal unavailable")
            self.append_shell("No supported local terminal emulator was found.\n")
            self.append_shell(f"SSH command: {ssh_command}\n")
            if command_to_copy:
                self.append_shell(f"Command to run after login: {command_to_copy}\n")
            else:
                self.append_shell("SSH command copied to clipboard.\n")
            return

        try:
            subprocess.Popen(launcher_args, cwd=REPO_ROOT, start_new_session=True)
        except Exception as error:  # noqa: BLE001 - GUI boundary should show launch failures.
            self.shell_status.set("terminal failed")
            self.append_shell(f"Could not open local terminal: {error}\n")
            self.append_shell(f"SSH command: {ssh_command}\n")
            return

        self.shell_status.set(f"terminal:{workstream.name}")
        self.append_shell(f"Opened local terminal for {workstream.name} on port {workstream.tunnel_port}.\n")
        if command_to_copy:
            self.append_shell("Command copied to clipboard. Paste it in the terminal when the runner prompt is ready.\n")
        self.log(f"Opened local terminal for {workstream.name}")

    def read_shell_output(self, session_id: int, fd: int, process: subprocess.Popen[Any]) -> None:
        while process.poll() is None:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            self.enqueue(lambda text=text, session_id=session_id: self.append_shell(text) if session_id == self.shell_session_id else None)
        if session_id == self.shell_session_id:
            try:
                os.close(fd)
            except OSError:
                pass
        self.enqueue(lambda session_id=session_id: self.mark_shell_disconnected(session_id))

    def mark_shell_disconnected(self, session_id: int) -> None:
        if session_id != self.shell_session_id:
            return
        self.shell_master_fd = None
        self.shell_process = None
        self.shell_status.set("disconnected")

    def write_shell_input(self, data: bytes) -> bool:
        if not self.shell_is_connected() or self.shell_master_fd is None:
            self.append_shell("Connect to a runner first.\n")
            self.shell_status.set("disconnected")
            return False
        try:
            os.write(self.shell_master_fd, data)
        except OSError as error:
            self.append_shell(f"Could not write to shell: {error}\n")
            self.shell_status.set("failed")
            return False
        return True

    def interrupt_shell(self) -> None:
        if self.write_shell_input(b"\x03"):
            self.log("Sent Ctrl-C to shell")

    def reconnect_shell(self) -> None:
        self.append_shell("Reconnecting shell...\n")
        self.disconnect_shell(log_message=False)
        self.root.after(250, self.connect_shell)

    def send_command_to_shell(self, command: str) -> None:
        self.open_embedded_terminal_shell(command_to_copy=command)

    def open_auth_command_shell(self, tool: ToolModule) -> None:
        # Poll the runner that launched the login even if the picker changes later.
        workstream = self.selected_workstream_item()
        remote_command = f"{tool.auth}; exec ${{SHELL:-/bin/bash}} -l"
        if self.embedded_terminal_is_running():
            self.open_system_terminal_shell(command_to_copy=tool.auth, remote_command=remote_command)
        else:
            self.open_embedded_terminal_shell(command_to_copy=tool.auth, remote_command=remote_command)
        self.set_tool_status(tool.module_id, "continue login")
        self.start_auth_polling(tool, workstream)
        self.log(f"Opened {tool.label} login shell. Continue the prompt in the terminal.")

    def open_copilot_setup_shell(self) -> None:
        remote_command = f"{COPILOT_SETUP_COMMAND}; exec ${{SHELL:-/bin/bash}} -l"
        self.open_embedded_terminal_shell(command_to_copy=COPILOT_SETUP_COMMAND, remote_command=remote_command)
        self.set_tool_status("copilot", "continue setup")
        self.log("Opened Copilot setup shell. Continue the gh copilot prompt in the terminal.")

    def disconnect_shell(self, log_message: bool = True) -> None:
        process = self.shell_process
        master_fd = self.shell_master_fd
        self.shell_session_id += 1
        if process and process.poll() is None:
            process.terminate()
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        self.shell_master_fd = None
        self.shell_process = None
        self.shell_status.set("disconnected")
        if log_message and hasattr(self, "shell_text"):
            self.append_shell("Shell disconnected.\n")

    def toggle_bridge(self) -> None:
        if self.bridge_enabled.get():
            self.start_bridge()
        else:
            self.stop_bridge()

    def start_bridge(self) -> None:
        def action() -> str:
            run_process(["bash", "./test-assets/generate-keys.sh"], timeout=120)
            run_process(["docker", "compose", "up", "--build", "-d"], timeout=600)
            run_process(
                self.ssh_args(self.config.repository.gateway_host_port, self.config.repository.gateway_user)
                + ["echo local-gateway-ok && whoami"],
                timeout=30,
            )
            return "gateway running"

        def done(_: str) -> None:
            self.bridge_enabled.set(True)
            self.set_bridge_status("running")

        self.set_bridge_status("starting")
        if self.workflow_sessions:
            self.log("Gateway start may interrupt active tunnels; if probe fails, start a fresh session or use a newer reconnecting workflow.")
        self.run_background("Start bridge", action, done, lambda _detail: self.set_bridge_status("failed"))

    def stop_bridge(self) -> None:
        def action() -> str:
            run_process(["docker", "compose", "down"], timeout=120)
            return "gateway stopped"

        def done(_: str) -> None:
            self.bridge_enabled.set(False)
            self.set_bridge_status("stopped")

        self.set_bridge_status("stopping")
        self.run_background("Stop bridge", action, done, lambda _detail: self.set_bridge_status("failed"))

    def check_local_gateway(self) -> None:
        def action() -> str:
            completed = run_process(
                self.ssh_args(self.config.repository.gateway_host_port, self.config.repository.gateway_user)
                + ["echo local-gateway-ok && whoami"],
                timeout=30,
            )
            return completed.stdout.strip().replace("\n", " / ")

        def done(_: str) -> None:
            self.set_bridge_status("local OK")

        self.set_bridge_status("checking")
        self.run_background("Check local gateway", action, done, lambda _detail: self.set_bridge_status("failed"))

    def check_public_gateway(self) -> None:
        def action() -> str:
            gateway_host = public_ip()
            completed = run_process(
                [
                    "ssh",
                    "-i",
                    str(self.private_key_path()),
                    "-p",
                    str(self.config.repository.gateway_host_port),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "ConnectTimeout=15",
                    "-o",
                    "LogLevel=ERROR",
                    f"{self.config.repository.gateway_user}@{gateway_host}",
                    "echo public-gateway-ok && whoami",
                ],
                timeout=40,
            )
            return completed.stdout.strip().replace("\n", " / ")

        def done(_: str) -> None:
            self.set_bridge_status("public OK")

        self.set_bridge_status("checking")
        self.run_background("Check public gateway", action, done, lambda _detail: self.set_bridge_status("failed"))

    def start_workstream(self, workstream: Workstream) -> None:
        active_session = self.active_session_for_run(workstream.run_id)
        if active_session is not None:
            self.workstream_enabled[workstream.name].set(False)
            self.set_workstream_status(workstream.name, "run active")
            self.log(
                f"{workstream.name} is still linked to active run {workstream.run_id} on port {workstream.tunnel_port}. "
                "Use another workstream/port for a parallel session, or Cancel that session first."
            )
            return

        def action() -> str:
            if not TUNNEL_PORT_MIN <= workstream.tunnel_port <= TUNNEL_PORT_MAX:
                raise RuntimeError(
                    f"{workstream.name} tunnel port {workstream.tunnel_port} is outside "
                    f"the published range {TUNNEL_PORT_MIN}-{TUNNEL_PORT_MAX}"
                )
            public_key = self.public_key_path().read_text(encoding="utf-8").strip()
            private_key = self.private_key_path().read_text(encoding="utf-8")
            gateway_host = public_ip()
            run_process(["gh", "secret", "set", "GATEWAY_HOST", "--body", gateway_host], timeout=60)
            run_process(["gh", "secret", "set", "GATEWAY_PORT", "--body", self.config.repository.gateway_secret_port], timeout=60)
            run_process(["gh", "secret", "set", "GATEWAY_USER", "--body", self.config.repository.gateway_secret_user], timeout=60)
            run_process(["gh", "secret", "set", "GATEWAY_PRIVATE_KEY"], input_text=private_key, timeout=60)
            workflow_args = [
                "gh",
                "workflow",
                "run",
                self.config.repository.workflow,
                "--ref",
                workstream.branch,
                "-f",
                f"SSH_PUBLIC_KEY={public_key}",
                "-f",
                f"MAX_LIFETIME={workstream.max_lifetime}",
            ]
            dispatch_note = ""
            try:
                run_process(
                    workflow_args + ["-f", f"TUNNEL_PORT={workstream.tunnel_port}"],
                    timeout=60,
                )
            except CommandError as error:
                if not is_unexpected_tunnel_port_input(error):
                    raise
                if workstream.tunnel_port != 2222:
                    raise RuntimeError(
                        "The pushed GitHub workflow does not support TUNNEL_PORT yet. "
                        "Commit and push .github/workflows/debug-runner.yml before starting "
                        f"the {workstream.name} workstream on port {workstream.tunnel_port}."
                    ) from error
                run_process(workflow_args, timeout=60)
                dispatch_note = " using legacy workflow inputs"
            time.sleep(5)
            completed = run_process(
                [
                    "gh",
                    "run",
                    "list",
                    "--workflow",
                    self.config.repository.workflow,
                    "--limit",
                    "1",
                    "--json",
                    "databaseId,status,url,displayTitle",
                ],
                timeout=60,
            )
            latest_runs = json.loads(completed.stdout)
            if latest_runs:
                latest = latest_runs[0]
                workstream.run_id = str(latest.get("databaseId", ""))
                workstream.run_url = str(latest.get("url", ""))
                return f"run {workstream.run_id} started on port {workstream.tunnel_port}{dispatch_note}"
            return f"workflow requested on port {workstream.tunnel_port}{dispatch_note}"

        def done(result: str) -> None:
            self.workstream_enabled[workstream.name].set(True)
            self.set_workstream_status(workstream.name, result)
            self.root.after(500, self.refresh_sessions)

        def failed(detail: str) -> None:
            self.workstream_enabled[workstream.name].set(False)
            self.set_workstream_status(workstream.name, self.start_failure_status(detail))

        self.set_workstream_status(workstream.name, f"starting {workstream.name}")
        self.run_background(f"Start {workstream.name}", action, done, failed)

    def start_failure_status(self, detail: str) -> str:
        if "does not support TUNNEL_PORT" in detail or "Unexpected inputs provided" in detail:
            return "push workflow"
        return "failed"

    def cancel_workstream(self, workstream: Workstream) -> None:
        def action() -> str:
            if not workstream.run_id:
                return "no captured run ID"
            run_process(["gh", "run", "cancel", workstream.run_id], timeout=60)
            return f"cancel requested for {workstream.run_id}"

        def done(result: str) -> None:
            self.workstream_enabled[workstream.name].set(False)
            workstream.run_id = ""
            workstream.run_url = ""
            self.set_workstream_status(workstream.name, result)
            self.root.after(500, self.refresh_sessions)

        self.set_workstream_status(workstream.name, "canceling")
        self.run_background(
            f"Cancel {workstream.name}",
            action,
            done,
            lambda _detail: self.set_workstream_status(workstream.name, "failed"),
        )

    def session_label(self, session: WorkflowSession) -> str:
        title = session.title or "workflow run"
        branch = session.branch or "branch?"
        return self.compact_status(f"{session.run_id} {session.status} {branch} {title}", 58)

    def refresh_sessions(self) -> None:
        def action() -> str:
            completed = run_process(
                [
                    "gh",
                    "run",
                    "list",
                    "--workflow",
                    self.config.repository.workflow,
                    "--limit",
                    "30",
                    "--json",
                    "databaseId,status,conclusion,createdAt,url,displayTitle,headBranch",
                ],
                timeout=60,
            )
            runs = json.loads(completed.stdout)
            active_runs = [
                run
                for run in runs
                if str(run.get("status", "")).lower() in ACTIVE_RUN_STATUSES and not run.get("conclusion")
            ]
            return json.dumps(active_runs)

        def done(result: str) -> None:
            runs = json.loads(result)
            labels: list[str] = []
            self.workflow_sessions.clear()
            for run in runs:
                session = WorkflowSession(
                    run_id=str(run.get("databaseId", "")),
                    status=str(run.get("status", "unknown")),
                    branch=str(run.get("headBranch", "")),
                    title=str(run.get("displayTitle", "")),
                    url=str(run.get("url", "")),
                    created_at=str(run.get("createdAt", "")),
                )
                if not session.run_id:
                    continue
                label = self.session_label(session)
                suffix = 2
                unique_label = label
                while unique_label in self.workflow_sessions:
                    unique_label = self.compact_status(f"{label} #{suffix}", 58)
                    suffix += 1
                self.workflow_sessions[unique_label] = session
                labels.append(unique_label)
            if self.session_picker is not None:
                self.session_picker.configure(values=labels)
            self.selected_session.set(labels[0] if labels else "")
            self.log(f"Sessions refreshed: {len(labels)} active")

        self.run_background("Refresh sessions", action, done)

    def selected_session_item(self) -> WorkflowSession | None:
        return self.workflow_sessions.get(self.selected_session.get())

    def active_session_for_run(self, run_id: str) -> WorkflowSession | None:
        if not run_id:
            return None
        for session in self.workflow_sessions.values():
            if session.run_id == run_id:
                return session
        return None

    def workstream_for_session(self, session: WorkflowSession) -> Workstream:
        selected = self.selected_workstream_item()
        matches = [workstream for workstream in self.config.workstreams if workstream.branch == session.branch]
        if selected in matches or len(matches) != 1:
            return selected
        return matches[0]

    def resume_selected_session(self) -> None:
        session = self.selected_session_item()
        if session is None:
            self.log("No active workflow session selected")
            return
        workstream = self.workstream_for_session(session)
        self.selected_workstream.set(workstream.name)
        workstream.run_id = session.run_id
        workstream.run_url = session.url
        self.workstream_enabled[workstream.name].set(True)
        self.set_workstream_status(workstream.name, f"run {session.run_id} resumed")
        self.log(f"Resumed run {session.run_id} on {workstream.name}:{workstream.tunnel_port}")
        self.open_embedded_terminal_shell()
        self.probe_workstream(workstream)

    def cancel_selected_session(self) -> None:
        session = self.selected_session_item()
        if session is None:
            self.log("No active workflow session selected")
            return

        def action() -> str:
            run_process(["gh", "run", "cancel", session.run_id], timeout=60)
            return f"cancel requested for {session.run_id}"

        def done(result: str) -> None:
            for workstream in self.config.workstreams:
                if workstream.run_id == session.run_id:
                    self.workstream_enabled[workstream.name].set(False)
                    workstream.run_id = ""
                    workstream.run_url = ""
                    self.set_workstream_status(workstream.name, result)
            self.root.after(500, self.refresh_sessions)

        self.run_background("Cancel session", action, done)

    def probe_failure_status(self, detail: str) -> str:
        lower_detail = detail.lower()
        if "kex_exchange_identification" in lower_detail or "connection reset" in lower_detail:
            return "tunnel down"
        if "connection refused" in lower_detail:
            return "port closed"
        if "connection timed out" in lower_detail or "no route" in lower_detail:
            return "gateway down"
        if "permission denied" in lower_detail:
            return "auth rejected"
        return "probe failed"

    def probe_or_start_workstream(self, workstream: Workstream) -> None:
        current_status = self.workstream_status.get(workstream.name)
        if current_status is not None and current_status.get().startswith("starting"):
            self.log(f"{workstream.name} runner is already starting")
            return
        if not workstream.run_id:
            self.set_workstream_status(workstream.name, f"starting {workstream.name}")
            self.log(f"No linked run for {workstream.name}; starting a new {workstream.name} runner.")
            self.start_workstream(workstream)
            return
        self.probe_workstream(workstream)

    def probe_workstream(self, workstream: Workstream) -> None:
        def action() -> str:
            completed = self.run_on_runner(workstream, "echo runner-ok && whoami && hostname && pwd", timeout=40)
            return completed.stdout.strip().replace("\n", " / ")

        def done(result: str) -> None:
            self.set_workstream_status(workstream.name, result)

        def failed(detail: str) -> None:
            status = self.probe_failure_status(detail)
            self.set_workstream_status(workstream.name, status)
            if status == "tunnel down":
                self.log(f"{workstream.name}:{workstream.tunnel_port} tunnel is down or owned by a different run. Refresh sessions or start a fresh port.")

        self.set_workstream_status(workstream.name, "probing")
        self.run_background(
            f"Probe {workstream.name}",
            action,
            done,
            failed,
        )

    def copy_ssh_command(self, workstream: Workstream) -> None:
        command = (
            f"ssh -i {self.config.repository.private_key_path} -p {workstream.tunnel_port} "
            "-o IdentitiesOnly=yes -o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
            "-o LogLevel=ERROR runner@localhost"
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self.log(f"Copied SSH command for {workstream.name}")

    def sync_github_auth_to_runner(self, tool: ToolModule) -> None:
        if tool.module_id == "copilot":
            self.open_copilot_setup_shell()
            return

        def action() -> str:
            workstream = self.selected_workstream_item()
            self.sync_github_auth_for_workstream(workstream)
            return "github ready"

        def done(result: str) -> None:
            self.set_tool_status(tool.module_id, result)

        self.set_tool_status(tool.module_id, "fixing")
        self.run_background(f"Fix {tool.label}", action, done, lambda _detail: self.set_tool_status(tool.module_id, "needs fix"))

    def sync_github_auth_for_workstream(self, workstream: Workstream) -> None:
        try:
            self.run_on_runner(workstream, "GH_PROMPT_DISABLED=1 gh auth status >/dev/null 2>&1", timeout=30)
            return
        except CommandError:
            pass

        token = run_process(["gh", "auth", "token"], timeout=30).stdout.strip()
        if not token:
            raise RuntimeError("local gh auth token is unavailable; run gh auth login locally first")
        self.run_on_runner(
            workstream,
            "GH_PROMPT_DISABLED=1 gh auth login --hostname github.com --with-token >/dev/null 2>&1",
            timeout=60,
            input_text=f"{token}\n",
        )
        self.run_on_runner(workstream, "GH_PROMPT_DISABLED=1 gh auth status >/dev/null 2>&1", timeout=30)

    def ask_copilot_for_tool_fix(self, tool: ToolModule) -> None:
        failure = self.last_tool_failure.get(tool.module_id, "The tool check or setup did not complete successfully.")
        setup_description = "manual terminal setup via gh copilot" if tool.module_id == "copilot" else repr(tool.install)
        prompt = (
            f"On an Ubuntu GitHub Actions runner, troubleshoot {tool.label}. "
            f"The check command is: {tool.check!r}. The setup command is: {setup_description}. "
            f"Last failure: {failure}. Suggest one safe shell command or next check."
        )

        def action() -> str:
            workstream = self.selected_workstream_item()
            self.sync_github_auth_for_workstream(workstream)
            command = "GH_PROMPT_DISABLED=1 gh copilot -- -p " + shlex.quote(prompt) + " --silent --no-color --output-format text"
            try:
                completed = self.run_on_runner(workstream, command, timeout=120)
            except CommandError:
                return "GitHub Copilot CLI is not ready. Run Fix for GitHub Copilot CLI, then retry this action."
            output = completed.stdout.strip() or completed.stderr.strip()
            return output or "Copilot did not return a suggestion"

        def done(result: str) -> None:
            status = "needs setup" if "is not installed" in result else "copilot tip ready"
            self.set_tool_status(tool.module_id, status)
            self.append_shell("\n# Copilot suggestion\n" + result + "\n")
            if self.notebook is not None and self.shell_tab is not None:
                self.notebook.select(self.shell_tab)

        self.set_tool_status(tool.module_id, "asking copilot")
        self.run_background(
            f"Copilot fix {tool.label}",
            action,
            done,
            lambda _detail: self.set_tool_status(tool.module_id, "needs fix"),
        )

    def evaluate_tool_check(self, tool: ToolModule, workstream: Workstream | None = None, record_failure: bool = True) -> str:
        if not tool.check:
            return "no check"
        try:
            if tool.target == "runner":
                completed = self.run_on_runner(workstream or self.selected_workstream_item(), tool.check, timeout=80)
            else:
                completed = run_shell(tool.check, timeout=80)
        except CommandError as error:
            if record_failure:
                self.last_tool_failure[tool.module_id] = (error.stderr or error.stdout or str(error))[:1200]
            return "needs fix"
        output = (completed.stdout or completed.stderr).strip()
        if record_failure:
            self.last_tool_failure.pop(tool.module_id, None)
        return output.splitlines()[0] if output else "ready"

    def start_auth_polling(self, tool: ToolModule, workstream: Workstream) -> None:
        generation = self.auth_poll_generations.get(tool.module_id, 0) + 1
        self.auth_poll_generations[tool.module_id] = generation

        def worker() -> None:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                time.sleep(6)
                if self.auth_poll_generations.get(tool.module_id) != generation:
                    return
                result = self.evaluate_tool_check(tool, workstream, record_failure=False)
                should_continue = result == "needs login"
                is_ready = "ready" in result.lower()

                def apply_result(result: str = result, should_continue: bool = should_continue, is_ready: bool = is_ready) -> None:
                    if self.auth_poll_generations.get(tool.module_id) != generation:
                        return
                    self.set_tool_status(tool.module_id, result)
                    if is_ready:
                        self.auth_poll_generations.pop(tool.module_id, None)
                        self.log(f"{tool.label} login detected on {workstream.name}")
                    elif not should_continue:
                        self.auth_poll_generations.pop(tool.module_id, None)
                        self.log(f"{tool.label} login monitor stopped: {result}")

                self.enqueue(apply_result)
                if is_ready or not should_continue:
                    return
            self.enqueue(lambda: self.log(f"{tool.label} login monitor timed out; click Check after finishing login."))

    def check_tool(self, tool: ToolModule) -> None:
        if not tool.check:
            self.log(f"{tool.label} has no check command")
            return

        def action() -> str:
            return self.evaluate_tool_check(tool)

        def done(result: str) -> None:
            self.set_tool_status(tool.module_id, result)

        self.set_tool_status(tool.module_id, "checking")
        self.run_background(
            f"Check {tool.label}",
            action,
            done,
            lambda _detail: self.set_tool_status(tool.module_id, "failed"),
        )

    def install_tool(self, tool: ToolModule) -> None:
        if tool.module_id == "copilot":
            self.open_copilot_setup_shell()
            return
        if not tool.install:
            self.set_tool_status(tool.module_id, "nothing to setup")
            self.log(f"{tool.label} has no headless setup command")
            return

        def action() -> str:
            try:
                if tool.target == "runner":
                    workstream = self.selected_workstream_item()
                    completed = self.run_on_runner(workstream, tool.install, timeout=600)
                else:
                    completed = run_shell(tool.install, timeout=600)
            except CommandError as error:
                self.last_tool_failure[tool.module_id] = (error.stderr or error.stdout or str(error))[:1200]
                return "needs fix"
            output = (completed.stdout or completed.stderr).strip()
            self.last_tool_failure.pop(tool.module_id, None)
            return output.splitlines()[-1] if output else "setup done"

        def done(result: str) -> None:
            self.set_tool_status(tool.module_id, result)

        self.set_tool_status(tool.module_id, "setting up")
        self.run_background(
            f"Install {tool.label}",
            action,
            done,
            lambda _detail: self.set_tool_status(tool.module_id, "needs fix"),
        )

    def copy_auth_command(self, tool: ToolModule) -> None:
        if tool.module_id == "copilot":
            self.open_copilot_setup_shell()
            return
        if not tool.auth:
            self.log(f"{tool.label} has no auth command")
            return
        if tool.auth == "sync-github-token":
            self.sync_github_auth_to_runner(tool)
            return
        if tool.auth.startswith("copilot:"):
            self.ask_copilot_for_tool_fix(tool)
            return
        if tool.target == "runner":
            if tool.module_id in TERMINAL_AUTH_MODULES:
                self.open_auth_command_shell(tool)
                return
            self.set_tool_status(tool.module_id, "command copied")
            self.send_command_to_shell(tool.auth)
            self.log(f"Copied {tool.label} fix command for the Shell tab.")
            if self.last_tool_failure.get(tool.module_id):
                self.ask_copilot_for_tool_fix(tool)
            return
        target = "local terminal"
        self.root.clipboard_clear()
        self.root.clipboard_append(tool.auth)
        self.set_tool_status(tool.module_id, "fix copied")
        self.log(f"Copied {tool.label} fix command. Run it directly in the {target}; do not paste tokens into chat.")


def run_config_check() -> int:
    config = load_config()
    duplicate_names = sorted(
        name
        for name in {workstream.name.lower() for workstream in config.workstreams}
        if sum(1 for workstream in config.workstreams if workstream.name.lower() == name) > 1
    )
    if duplicate_names:
        print(f"duplicate runner names: {duplicate_names}", file=sys.stderr)
        return 2
    duplicate_ports = sorted(
        port
        for port in {workstream.tunnel_port for workstream in config.workstreams}
        if sum(1 for workstream in config.workstreams if workstream.tunnel_port == port) > 1
    )
    if duplicate_ports:
        print(f"duplicate tunnel ports: {duplicate_ports}", file=sys.stderr)
        return 2
    invalid_ports = [
        f"{workstream.name}:{workstream.tunnel_port}"
        for workstream in config.workstreams
        if not TUNNEL_PORT_MIN <= workstream.tunnel_port <= TUNNEL_PORT_MAX
    ]
    if invalid_ports:
        print(
            f"tunnel ports must be in published range {TUNNEL_PORT_MIN}-{TUNNEL_PORT_MAX}: "
            + ", ".join(invalid_ports),
            file=sys.stderr,
        )
        return 2
    print(f"config-ok workstreams={len(config.workstreams)} tools={len(config.tools)}")
    print(f"repo-root={REPO_ROOT}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Native helper widget for debug-hosted-runner")
    parser.add_argument("--check", action="store_true", help="validate widget config without opening the GUI")
    args = parser.parse_args()
    if args.check:
        return run_config_check()

    config = load_config()
    root = tk.Tk()
    RunnerWidget(root, config)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
