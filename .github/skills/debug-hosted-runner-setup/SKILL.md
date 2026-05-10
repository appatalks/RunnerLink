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
- Reverse tunnel host/container port: `2222`
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
     -f MAX_LIFETIME=3600
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

Install GitHub Copilot CLI on the runner as the `gh-copilot` extension only after the tunnel works.

```bash
ssh -i test-assets/id_rsa_test -p 2222 \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  runner@localhost 'bash -s' <<'REMOTE_SCRIPT'
set -u
if ! command -v gh >/dev/null 2>&1; then
  echo 'gh is not installed on the runner'
  exit 2
fi
if gh extension list 2>/dev/null | awk '{print $1}' | grep -Fxq 'gh-copilot'; then
  gh extension upgrade gh-copilot || true
else
  gh extension install github/gh-copilot
fi
gh copilot --help | head -n 60
REMOTE_SCRIPT
```

Using `gh copilot` may require the runner to authenticate with a GitHub account or token that has Copilot access. Do not handle that token through chat.

## Troubleshooting Cues

- Local gateway SSH fails on 50556: rebuild the gateway and confirm `authorized_keys` exists before build.
- Public gateway SSH fails with connection refused: check firewall, router forwarding, Docker port publishing, and current public IP.
- Workflow hangs before tunnel readiness: add or confirm SSH `ConnectTimeout` and `ConnectionAttempts`; inspect gateway logs for an accepted `gateway` public key.
- Port 2222 refuses or resets: verify the workflow is still in the tunnel step and that the gateway has an active reverse listener.
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
- Add a workflow input for tunnel port if 2222 is already in use.
- Add `actionlint` and `shellcheck` to CI.
- Add a short `TESTING.md` with local, public, and GitHub-runner verification paths.
- Add a preflight command that reports firewall/router readiness without revealing public IPs.