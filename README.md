# DeepCerebra Bridge Connector (`dcc-bridge`)

Relay your **local GPU** models (Ollama and/or LM Studio) into the DeepCerebra
Coder **web app** — so the browser can run models on *your* machine, exactly like
the desktop app does. Inference is relayed gateway → connector over an
authenticated WebSocket and **never touches the cloud engine**.

```
Browser ──SSE──> Gateway ──WS──> dcc-bridge (this) ──HTTP──> Ollama / LM Studio (your GPU)
```

The same connector also provides **local execution**: the web terminal and the
agent's build/test commands can be relayed here and run on *your* machine. Two
scopes, chosen by you when starting the connector:

- **Workspace (default, safest)** — everything is **confined to a dedicated
  workspace folder** (default `~/DeepCerebra`; the connector refuses any path
  that escapes it). Used automatically as a fallback when the hosted deployment
  has no server-side Docker sandbox.
- **Host access (opt-in)** — grant real project directories with `--host-dir`
  (repeatable) or the whole machine with `--allow-any-dir`. The web app can then
  run commands *in your actual folders*, so CLI tools you already configured
  locally — **Railway, AWS CLI, OVH CLI, gh, docker, kubectl, WSL**, … — work
  with their existing credentials, exactly as in your own terminal. In the web
  app, open the terminal's **Execution target** popover and pick **My computer**
  (and optionally a working directory).

Either way this works from any device: the web app on a phone or tablet simply
relays to whichever of your paired computers is online. Local execution is on by
default; start with `--no-exec` to make a device inference-only. File syncs from
the browser always land only in the confined workspace — never in host folders.

## Requirements

- Python 3.10+
- A local model server running:
  - **Ollama** — `ollama serve` (default `http://localhost:11434`), and at least one `ollama pull <model>`
  - and/or **LM Studio** — Developer tab → **Start Server** (default `http://localhost:1234`), with a model loaded

## Install

Install as a package so `python -m dcc_bridge` (and the `dcc-bridge` command)
work from **any** directory — required if you'll grant host folders elsewhere
on disk:

```bash
# easiest — standalone mirror repo (no need to clone the full DeepCerebra repo)
pip install git+https://github.com/mohammadkhair7/DeepCerebra-connector

# or from this repo root; -e = editable, so `git pull` updates it in place
pip install -e connector
```

On Windows with several Pythons, install into the interpreter your console
actually uses, e.g. `C:\Python314\python.exe -m pip install -e connector`.

Full setup + usage guide: `docs/DEEPCEREBRA_CONSOLE_BRIDGE.md`. Standalone
distribution (this folder mirrored):
<https://github.com/mohammadkhair7/DeepCerebra-connector>.

## Pair & run

1. In the web app, open **Settings → Local GPU → Add this computer**. Copy the
   one-time connector **token** (`dcc_brg_…`).
2. Start the connector:

```bash
# via flags
python -m dcc_bridge --gateway wss://your-gateway-host --token dcc_brg_xxxxx

# or via environment variables
export DCC_BRIDGE_GATEWAY=wss://your-gateway-host
export DCC_BRIDGE_TOKEN=dcc_brg_xxxxx
python -m dcc_bridge
```

Your local models now appear in the web model picker under **Local (your GPU)**.

## Options

| Flag | Env | Default | Purpose |
|---|---|---|---|
| `--gateway` | `DCC_BRIDGE_GATEWAY` | — | Gateway URL (`wss://…` or `https://…`) |
| `--token` | `DCC_BRIDGE_TOKEN` | — | Connector token from the web app |
| `--ollama-host` | `OLLAMA_HOST` | `http://localhost:11434` | Ollama server |
| `--lmstudio-host` | `LMSTUDIO_HOST` | `http://localhost:1234` | LM Studio server |
| `--workspace` | `DCC_BRIDGE_WORKSPACE` | `~/DeepCerebra` | Folder local commands are confined to |
| `--host-dir PATH` | `DCC_BRIDGE_HOST_DIRS` (path-sep list) | none | Grant a REAL directory the web app may run commands in (repeatable) |
| `--allow-any-dir` | `DCC_BRIDGE_ALLOW_ANY_DIR=true` | off | Allow commands anywhere on this machine (full host access — use with care) |
| `--no-exec` | `DCC_BRIDGE_ALLOW_EXEC=false` | exec enabled | Disable local command execution (inference relay only) |

Examples:

```bash
# Let the web app use your pre-configured CLIs inside two project folders
python -m dcc_bridge --gateway wss://YOUR_HOST --token dcc_brg_xxx \
  --host-dir ~/projects/api --host-dir ~/projects/web

# Windows: grant a projects folder (PowerShell)
python -m dcc_bridge --gateway wss://YOUR_HOST --token dcc_brg_xxx --host-dir "F:\Projects"
```

Security model for host access: the web app can only ever do what *you* granted
on this command line — nothing is exposed by default beyond the confined
workspace, browser file syncs never write into host folders, and the active
scope + granted roots are printed at startup and shown per device in the web
app (Settings → Local machine).

The connector auto-reconnects with backoff, re-discovers models every 60s, and
honors cancellation. Revoke a device anytime from **Settings → Local GPU**.

## Notes

- The bridge must be enabled on the server (`DCC_ENABLE_BRIDGE=true`).
- The live connection registry is in-process; multi-replica gateways need sticky
  sessions. See `docs/LOCAL_GPU_AND_LMSTUDIO_PLAN.md`.
