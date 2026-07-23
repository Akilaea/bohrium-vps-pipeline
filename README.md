# Bohrium VPS Pipeline

Register → create Bohrium node → SSH → run C3Pool miner.

## Local

```bash
pip install -r requirements.txt
python vps.py --no-proxy
# or
python vps.py --count 5 --workers 5 --retries 2 --no-proxy
```

| Flag | Default |
|------|---------|
| `--count` | 20 |
| `--workers` | min(count, 20) |
| `--retries` | 2 |
| `--wallet` | env `VPS_WALLET` or built-in |
| `--proxy` | `http://127.0.0.1:7890` (use `--no-proxy` if none) |

## GitHub Actions

Workflow: **VPS Pipeline** (`workflow_dispatch`).

1. Repo → **Actions** → **VPS Pipeline** → **Run workflow**
2. Optional inputs: `count`, `workers`, `retries`, `wallet`
3. Artifacts: `vps-result` (JSON summary)

```bash
gh workflow run vps.yml -f count=1 -f workers=1 -f retries=1
```

## Modules

- `vps.py` — orchestrator
- `bohrium_register.py` — email register / login
- `bohrium_create_node.py` — create node
- `bohrium_ssh.py` — SSH helper
