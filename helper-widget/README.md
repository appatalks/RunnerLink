# RunnerLink

RunnerLink is a native Linux/macOS desktop app for direct access to GitHub Actions runners via reverse-SSH tunnels. It uses Python and Tk, so it does not need a web server or packaging step.

## Run

```bash
python3 helper-widget/runner_widget.py
```

Validate the config without opening a window:

```bash
python3 helper-widget/runner_widget.py --check
```

## What It Controls

- Bridge: turn the Docker gateway on or off, then check local or public reachability.
- Runners: add and start named workstreams, probe SSH, copy SSH commands, cancel explicitly, or refresh/resume/cancel active workflow sessions.
- Tools: check readiness, run headless setup, or open/copy the safest available login command when credentials are required. Copilot uses a manual terminal setup flow.
- Shell: dock a real terminal emulator inside the app on Linux, or fall back to an external terminal window when docking is unavailable.

The Controls tab keeps a live Activity pane on the right so bridge, runner, and tool output is visible while you operate the controls. Status dots use green for ready/healthy, yellow for pending/unknown, and red for failed/stopped/needs-fix states.

The Controls tab uses a draggable split between operations and Activity. Drag the divider to give more room to the runner/tool rows or to the live log. The separate Activity tab was removed because the same log is now always visible beside the controls.

The widget opens in a dark theme by default. Compact circular controls use drawn icons instead of letter labels, and each control shows a descriptive tooltip on hover.

## Multiple Workstreams

The widget reads workstreams from `helper-widget/config.example.json`, or from ignored local file `helper-widget/config.json` if present. The default runner is `main` on port `2222`.

Use Add Runner to create another named workstream. Names are normalized to letters, numbers, dashes, and underscores. The widget assigns the first free local tunnel port in `2222-2231`, saves the updated workstream list to ignored `helper-widget/config.json` with an atomic write, and starts the new runner immediately. Probe the runner after it starts to confirm the assigned port is actually free on the gateway.

Each workstream needs a unique tunnel port. The default Compose configuration exposes `2222-2231`, and the workflow accepts `TUNNEL_PORT` as an input.

If the pushed GitHub workflow does not support `TUNNEL_PORT` yet, the widget retries the default `2222` workstream with legacy inputs. Additional workstream ports require committing and pushing the workflow update first; otherwise the runner status will show `push workflow`.

Runner rows do not have a start toggle. Add Runner starts new named runners immediately. For the default `main` runner, the first Probe starts a new `main` runner when no run is linked yet; later Probe clicks check the existing SSH tunnel. Use the workstream stop button or the sessions picker Cancel button when you intentionally want to stop a runner. To keep an older session alive while starting another one, use a different workstream with a different tunnel port. Two live runners cannot share the same reverse-tunnel port.

The sessions picker under Runners refreshes active runs for this workflow. Resume binds the selected run to the currently selected workstream/port, docks the runner shell, and probes SSH. Cancel stops the selected workflow run. GitHub's run list does not reliably expose the reverse-tunnel port, so choose the workstream that owns the port before resuming a session.

If an active run resumes but probing reports `tunnel down`, the workflow may still be alive while its reverse tunnel was severed by a gateway restart or bound to a different port. New workflow runs reconnect automatically when the gateway comes back. Older runs may need a fresh session on a free port, or an explicit cancel if you no longer need them.

## Tool Modules

Tool modules are data-driven. Add a new entry to `tools` with:

- `id`: stable module id
- `label`: display label
- `target`: `runner` or `local`
- `check`: non-interactive command used by the Check button; prefer one-line friendly statuses such as `ready` or `needs login`
- `install`: optional non-interactive command used by the Setup button
- `auth`: `sync-github-token` for GitHub headless auth sync, or a manual command opened by the Fix button. Copilot ignores `auth` and uses Setup to start `gh copilot` in the docked terminal.

On Linux/X11 or XWayland with `xterm` installed, the terminal button docks `xterm` into the Shell tab with the same SSH command, so readline, prompts, copy/paste, cursor movement, and full-screen tools are handled by a real terminal emulator without leaving the app. The docked terminal uses conservative row and column sizing from the visible pane when launched; after resizing the app, close and dock the terminal again to relaunch with the new dimensions. It is configured to copy selected text to the clipboard and to paste with `Ctrl+Shift+V` or `Shift+Insert`. On macOS, Wayland sessions without XWayland, machines without `xterm`, or auth flows launched while a docked shell is already active, the widget opens a local terminal window instead.

The widget never runs login prompts in the background and never asks for tokens or passwords. For GitHub, Fix uses your local `gh auth token` and sends it directly to the selected runner through `gh auth login --with-token`, without printing or copying the token. For Copilot, Setup docks the selected runner shell and starts `gh copilot`; continue the installer prompt in the terminal. For Azure and Azure Data Explorer, Setup installs or repairs the command modules, while Fix starts `az login --use-device-code --allow-no-subscriptions` in a runner shell and polls the selected runner until Check reports ready. Azure login uses `--allow-no-subscriptions` so tenant-only accounts can authenticate cleanly.

If Azure CLI reports missing Python modules or command modules during login, run Setup for Azure CLI before Fix. Setup installs or repairs Azure CLI on the runner, then reports `azure ready` or `needs login`. Azure Data Explorer setup also ensures Azure CLI is present before installing the `kusto` extension, then reports `adx ready` or `needs login`. Checks suppress Azure JSON output, so a status of `{` means the local config is stale.

The Copilot CLI check verifies the installed command with `gh copilot -- --help`. Setup opens the docked terminal and starts `gh copilot` on the runner so the user can approve the GitHub CLI installer prompt directly. The widget manages the GitHub CLI extension command `gh copilot`; it does not install the unrelated standalone `copilot` snap command.

When a runner-side tool check or setup fails, Fix also asks `gh copilot` on the runner for a troubleshooting command and prints the suggestion in the Shell tab when Copilot is available. Copilot suggestions are shown for review and are not auto-executed.

Runner SSH probes disable host-key persistence because GitHub-hosted runners are ephemeral and their host keys change per workflow run.

## Packaging Direction

This first slice is intentionally dependency-light. The same module model can later be wrapped as a menu bar/tray app with Tauri, PyInstaller, or platform-specific launchers once the workflows settle.
