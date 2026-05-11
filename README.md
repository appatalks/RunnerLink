# Debug GitHub Hosted Runner through an SSH Gateway

This repository enables SSH access to GitHub-hosted runners using a Docker-based SSH gateway and reverse SSH tunnels.

## Architecture

GitHub-hosted runners cannot accept inbound SSH connections. The solution uses a **reverse SSH tunnel**:

1. **Gateway Server** (Docker container) - SSH server on your local machine
2. **GitHub Runner** - Connects to gateway with reverse tunnel: `-R [tunnel-port]:localhost:22`
3. **You** - SSH to the matching gateway tunnel port, which tunnels through to the GitHub runner

```
Your Machine → ssh gateway:[2222-2231] → (reverse tunnel) → GitHub Runner:22
```

## Setup

### 1. Start the Docker Gateway

```bash
# Generate the local test key and gateway authorized_keys file
bash ./test-assets/generate-keys.sh

# Build and start the gateway container
docker compose up --build -d

# Verify it's running
docker compose ps
docker compose logs -f gateway
```

The gateway will listen on:
- **Port 50556** - Gateway SSH (for GitHub runner to connect)
- **Ports 2222-2231** - Reverse tunnel endpoints (for you to connect to runners)

### 2. Configure Router Port Forwarding

For GitHub runners and your connections to reach the gateway, expose both ports:

1. Find your local IP: `ip addr show | grep "inet "`
2. Configure your router to forward both ports:
   - External Port: **50556** → Internal IP: **[your-local-ip]** → Internal Port: **50556** (for GitHub runner to connect)
   - External Ports: **2222-2231** → Internal IP: **[your-local-ip]** → Internal Ports: **2222-2231** (for you to connect to runners)
3. Get your public IP: `curl icanhazip.com`

### 3. Set Up GitHub Secrets

In your GitHub repository, go to **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret Name | Value | Example |
|------------|-------|---------|
| `GATEWAY_PRIVATE_KEY` | Contents of `test-assets/id_rsa_test` | Entire file including BEGIN/END lines |
| `GATEWAY_HOST` | Your public IP address | `123.45.67.89` |
| `GATEWAY_PORT` | Gateway SSH port | `50556` |
| `GATEWAY_USER` | Gateway username | `gateway` |

**To get the private key:**
```bash
cat test-assets/id_rsa_test
# Copy the entire output including -----BEGIN OPENSSH PRIVATE KEY-----
```

### 4. Run the GitHub Actions Workflow

Drop `debug-runner.yml` into `.github/workflows/`. (Optionally add your custom steps into the placeholder section) 

1. Go to your repository → **Actions** → **SSH into GitHub Runner**
2. Click **Run workflow**
3. Provide these inputs:
   - **SSH_PUBLIC_KEY**: Your local public key (e.g., `cat ~/.ssh/id_rsa.pub`) or use test key: `cat test-assets/id_rsa_test.pub`
   - **MAX_LIFETIME**: How long to keep tunnel open in seconds (default: 3600)
   - **TUNNEL_PORT**: Gateway reverse tunnel port for this runner (default: 2222, supported range: 2222-2231)
4. Click **Run workflow**

### 5. Connect to the GitHub Runner

Once the workflow shows "Tunnel is active!", connect:

```bash
# Generate and use the test key
bash ./test-assets/generate-keys.sh
ssh -i test-assets/id_rsa_test -p 2222 -o IdentitiesOnly=yes -o UserKnownHostsFile=/dev/null runner@[YOUR_PUBLIC_IP]

# Using your own key (if you provided your own public key)
ssh -o UserKnownHostsFile=/dev/null -p 2222 runner@[YOUR_PUBLIC_IP]
```

You're now inside the GitHub-hosted runner!

## Testing the Gateway Locally

Test that the gateway is working before running the GitHub workflow:

```bash
# Connect to gateway user (not runner)
ssh -i test-assets/id_rsa_test -p 50556 -o IdentitiesOnly=yes gateway@localhost

# Expected output: Ubuntu shell as user "gateway"
```

## Troubleshooting

### Connection refused on port 50556
- Check gateway is running: `docker compose ps`
- Check router port forwarding is configured correctly
- Test externally: `ssh -i test-assets/id_rsa_test -p 50556 gateway@[YOUR_PUBLIC_IP]`

### Permission denied when connecting to runner
- Make sure you're using the correct private key (matching the public key you provided to the workflow)
- Check the workflow is still running (tunnel only exists while job is active)
- Connect to port **2222** not 50556: `ssh -p 2222 runner@...`

### Multiple runner workstreams
- Start each workflow run with a unique `TUNNEL_PORT` in the published range, such as 2222, 2223, or 2224.
- Connect to the matching port for that runner: `ssh -p 2223 runner@...`
- RunnerLink (`helper-widget/`) can launch, probe, and cancel workstreams from a native Linux/macOS desktop UI.

### Workflow tunnel fails to establish
- Verify GitHub Secrets are set correctly
- Check gateway logs: `docker logs debug-runner-gateway-1 --tail 50`
- Ensure `GATEWAY_PRIVATE_KEY` secret contains the entire key including BEGIN/END lines

## How It Works

1. **Gateway Container**: Runs OpenSSH server with `GatewayPorts yes` to allow reverse tunnels
2. **GitHub Workflow**: 
   - Installs SSH server on the GitHub runner
   - Adds your public key to runner's `authorized_keys`
   - Opens reverse tunnel: `ssh -R [tunnel-port]:localhost:22 gateway@your-gateway`
   - Keeps job alive for MAX_LIFETIME seconds
3. **You Connect**: SSH to the matching gateway tunnel port, which forwards through to the runner

## Files

- `Dockerfile` - SSH gateway server (Ubuntu 22.04 + OpenSSH)
- `docker-compose.yml` - Gateway service with port mappings
- `authorized_keys` - Generated public keys authorized to connect to gateway
- `.github/workflows/debug-runner.yml` - GitHub Actions workflow for reverse tunnel
- `test-assets/` - Test SSH keys and key generation script
- `TESTING.md` - Comprehensive testing and troubleshooting guide

## Gateway Configuration

The SSH server is configured with:
- Port 50555 (internal) → 50556 (external host port)
- Ports 2222-2231 (reverse tunnel endpoints)
- `GatewayPorts yes` - Allows reverse tunnels
- `AllowTcpForwarding yes` - Enables TCP forwarding
- Public key authentication only (no passwords)
- User: `gateway`

## RunnerLink

Run RunnerLink locally:

```bash
python3 helper-widget/runner_widget.py
```

RunnerLink provides direct access to GitHub Actions runners with multiple workstreams, docked terminal sessions, file explorer, and modular tool auth for GitHub CLI, Copilot, Azure CLI, and Azure Data Explorer. Customize workstreams and tool modules by copying `helper-widget/config.example.json` to `helper-widget/config.json`.

## Screenshot

<img width="1601" height="654" alt="Screenshot 2025-12-04 at 8 29 52 AM" src="https://github.com/user-attachments/assets/6fe4b727-e4c4-4b3f-a414-12313a0e0191" />
