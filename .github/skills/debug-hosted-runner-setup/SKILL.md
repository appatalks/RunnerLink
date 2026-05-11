---
name: debug-hosted-runner-setup
description: "Use when setting up, rerunning, troubleshooting, or replicating reverse-SSH access to GitHub-hosted runners through a Docker OpenSSH gateway, including firewall checks, GitHub Actions secrets, tunnel verification, and optional runner tooling such as gh-copilot."
user-invocable: true
disable-model-invocation: false
---

# Debug Hosted Runner Setup

Use this skill to set up or reproduce a debug path into a GitHub-hosted runner through a local Docker OpenSSH gateway and reverse SSH tunnel.

The expected architecture is:

```text
local machine -> gateway container port 2222 -> reverse SSH tunnel -> GitHub runner port 22
GitHub runner -> gateway public host port 50556 -> gateway container port 50555
```

## Safety Rules

- Do not print private keys, GitHub tokens, public keys, public IPs, or secret values in chat output.
- Do not request tokens or passwords through chat. If authentication is required, tell the user to type it directly into the terminal or use `gh auth login` themselves.
- Keep generated SSH key material out of Git and Docker build context.
- Treat ports 50556 and 2222 as intentionally exposed. Confirm the user understands the operational exposure before broadening access.
- Prefer short, targeted checks over long-running public exposure tests.

## Project Defaults

- Gateway user: `gateway`
- Runner user: `runner`
- Gateway SSH host port: `50556`
- Gateway SSH container port: `50555`
- Reverse tunnel host/container port range: `2222-2231`
- Runner SSH target: `localhost:22`
- Local test key: `test-assets/id_rsa_test`
- Gateway authorized keys file: `authorized_keys`
- Workflow: `.github/workflows/debug-runner.yml`

## Setup Workflow

1. Generate or refresh local key material.

   ```bash
   bash ./test-assets/generate-keys.sh
   ```

2. Start or rebuild the gateway.

   ```bash
   docker compose up --build -d
   docker compose ps
   ```

3. Verify local gateway SSH.

   ```bash
   ssh -i test-assets/id_rsa_test -p 50556 \
     -o BatchMode=yes \
     -o IdentitiesOnly=yes \
     -o StrictHostKeyChecking=no \
     -o UserKnownHostsFile=/dev/null \
     -o ConnectTimeout=10 \
     gateway@localhost 'echo local-gateway-ok && whoami'
   ```

4. Verify public reachability after firewall/router changes.

   Do not print the public IP in chat output.

   ```bash
   public_ip=$(curl -fsS https://icanhazip.com | tr -d '\n')
   ssh -i test-assets/id_rsa_test -p 50556 \
     -o BatchMode=yes \
     -o IdentitiesOnly=yes \
     -o StrictHostKeyChecking=no \
     -o UserKnownHostsFile=/dev/null \
     -o ConnectTimeout=10 \
     gateway@"$public_ip" 'echo public-gateway-ok && whoami'
   ```

5. Set or refresh GitHub Actions secrets.

   ```bash
   public_ip=$(curl -fsS https://icanhazip.com | tr -d '\n')
   gh secret set GATEWAY_HOST --body "$public_ip"
   gh secret set GATEWAY_PORT --body "50556"
   gh secret set GATEWAY_USER --body "gateway"
   gh secret set GATEWAY_PRIVATE_KEY < test-assets/id_rsa_test
   ```

6. Trigger the workflow.

   ```bash
   gh workflow run debug-runner.yml \
     -f SSH_PUBLIC_KEY="$(cat test-assets/id_rsa_test.pub)" \
     -f MAX_LIFETIME=3600 \
     -f TUNNEL_PORT=2222
   ```

7. Track the run.

   ```bash
   gh run list --workflow debug-runner.yml --limit 1 \
     --json databaseId,status,conclusion,createdAt,url,displayTitle
   ```

8. Verify the reverse tunnel.

   ```bash
   ssh -i test-assets/id_rsa_test -p 2222 \
     -o BatchMode=yes \
     -o IdentitiesOnly=yes \
     -o StrictHostKeyChecking=no \
     -o UserKnownHostsFile=/dev/null \
     -o ConnectTimeout=15 \
     runner@localhost 'echo runner-ok && whoami && hostname && pwd'
   ```

9. Give the user the interactive connection command.

   ```bash
   ssh -i test-assets/id_rsa_test -p 2222 \
     -o IdentitiesOnly=yes \
     -o UserKnownHostsFile=/dev/null \
     runner@localhost
   ```

## Optional Runner Tooling

Use GitHub Copilot CLI on the runner only after the tunnel works. Prefer the helper widget's Copilot Setup button, which opens the selected runner shell with `gh copilot` so the user can approve any GitHub CLI prompt directly. Do not automate Copilot prompt approval or handle Copilot auth tokens through chat.

If working by hand, run the same command in the runner shell:

```bash
ssh -i test-assets/id_rsa_test -p 2222 \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  runner@localhost 'gh copilot'
```

Using `gh copilot` may require the runner to authenticate with a GitHub account or token that has Copilot access.

## RunnerLink

Use RunnerLink when the user wants a desktop control surface for direct access to Actions runners, multiple runner workstreams, and modular tool auth checks.

```bash
python3 helper-widget/runner_widget.py
```

RunnerLink reads workstreams and tool modules from `helper-widget/config.example.json`, or from ignored local file `helper-widget/config.json` if present. Do not store tokens or passwords in either file. Interactive auth for GitHub, Copilot, Azure, or Azure Data Explorer should happen directly in a trusted terminal or selected runner shell.

The default runner is `main` on reverse tunnel port `2222`. Use Add Runner in RunnerLink to create additional named runners; names are normalized, ports are auto-assigned from `2222-2231`, the updated workstream list is saved atomically to ignored `helper-widget/config.json`, and the runner starts immediately. Auto-assigned ports are based on local config only, so probe after starting a runner to confirm the gateway port is free.

Tool Setup remains non-interactive except for Copilot. Azure CLI and Azure Data Explorer Setup install or repair command modules only; Fix starts `az login --use-device-code --allow-no-subscriptions` in a runner shell and polls the selected runner until the check reports ready or a setup problem. Azure/ADX checks should print one friendly status line such as `azure ready`, `adx ready`, `needs login`, or `kusto missing`, never raw Azure JSON.

The docked `xterm` uses conservative geometry and does not live-resize perfectly inside Tk. If it clips after resizing, close and dock it again, or use the external terminal fallback. A future package can replace this with a proper VTE/Tauri terminal surface.

Runner rows do not use start toggles. Add Runner starts additional named runners immediately, and Probe starts the default `main` runner when no run is linked yet. Use the explicit workstream stop button or sessions picker Cancel button to call `gh run cancel`.

The sessions picker refreshes active workflow runs, resumes a selected run against the currently selected workstream/port, and cancels only the selected run. GitHub's run list does not expose the `TUNNEL_PORT` input, so choose the workstream that owns the port before resuming. To keep one runner alive while starting another, start a different workstream with a different tunnel port; two live tunnels cannot share the same reverse-forward port.

Current workflow runs supervise the reverse SSH tunnel and reconnect with capped backoff until `MAX_LIFETIME` expires. Older runs that predate this reconnecting workflow can still appear `in_progress` after a gateway restart even though their tunnel is gone.

## Troubleshooting Cues

- Local gateway SSH fails on 50556: rebuild the gateway and confirm `authorized_keys` exists before build.
- Public gateway SSH fails with connection refused: check firewall, router forwarding, Docker port publishing, and current public IP.
- Workflow hangs before tunnel readiness: add or confirm SSH `ConnectTimeout` and `ConnectionAttempts`; inspect gateway logs for an accepted `gateway` public key.
- Port 2222 refuses or resets: verify the workflow is still in the tunnel step and that the gateway has an active reverse listener. If the gateway restarted, an older run may still be alive while its tunnel is gone; start a fresh session on a free port or cancel the stale run.
- `gh run view --log` may not show live logs while a job is in progress; use gateway logs and an SSH probe to check readiness.
- Cancel stuck or finished debug runs with `gh run cancel <run-id>`.

## Validation Checklist

- `bash -n test-assets/generate-keys.sh`
- `bash ./test-assets/generate-keys.sh`
- `docker compose config`
- `docker compose build gateway`
- `git diff --check`
- `actionlint .github/workflows/debug-runner.yml` when available
- `shellcheck test-assets/generate-keys.sh` when available

## Future Enhancements To Consider

- Add a wrapper script for setup, secret refresh, workflow run, and tunnel probing.
- Add a cleanup script for canceling the run and stopping the gateway.
- Package RunnerLink as a macOS menu bar app or Linux tray app if the Python/Tk prototype becomes too limited.
- Add `actionlint` and `shellcheck` to CI.
- Add a short `TESTING.md` with local, public, and GitHub-runner verification paths.
- Add a preflight command that reports firewall/router readiness without revealing public IPs.
