# DeepCerebra Bridge Connector (`dcc-bridge`)

Connect **your own computer** to the DeepCerebra web app — [deepcerebra.ai](https://deepcerebra.ai)
or [deepcerebra.io](https://deepcerebra.io) — so it can:

1. **Run models on your GPU** — your local **Ollama** / **LM Studio** models
   appear in the web model picker under *Local (your GPU)*.
2. **Use your terminal / console** — the web terminal and the AI agent can run
   real commands on your machine, including CLI tools you already configured
   (**Railway, AWS CLI, OVH CLI, gh, docker, kubectl, WSL**, …) with their
   existing credentials.

```
Browser ──HTTPS──▶ DeepCerebra Gateway ──WebSocket──▶ dcc-bridge (this) ──▶ your GPU / your shell
```

The connector dials **out** over an authenticated WebSocket — no inbound ports,
works behind NAT and firewalls. Works on Windows, macOS, and Linux
(Python 3.10+).

## Install

```bash
pip install git+https://github.com/mohammadkhair7/DeepCerebra-connector
```

or clone + editable install (a later `git pull` updates it in place):

```bash
git clone https://github.com/mohammadkhair7/DeepCerebra-connector
pip install -e DeepCerebra-connector
```

> **Windows, multiple Pythons?** Install into the interpreter your console
> actually uses, e.g.
> `C:\Python314\python.exe -m pip install git+https://github.com/mohammadkhair7/DeepCerebra-connector`

## Pair & run

1. In the DeepCerebra web app, click the **laptop icon** in the left activity
   rail — **Local Machine (GPU + Console)** — then **Add this computer**. Copy
   the one-time token (`dcc_brg_…`) and the ready-to-paste command.
2. Run it on your computer:

```bash
# use the SAME domain you use in the browser:
python -m dcc_bridge --gateway wss://deepcerebra.ai --token dcc_brg_xxxxx   # Railway
python -m dcc_bridge --gateway wss://deepcerebra.io --token dcc_brg_xxxxx   # OVH (wss://api.deepcerebra.io also works)
```

Your device shows **online** on the Local Machine page within seconds.

> deepcerebra.ai and deepcerebra.io are **separate deployments with separate
> accounts** — pair on the one you actually use. There is no
> `api.deepcerebra.ai`; the `api.` subdomain exists only on deepcerebra.io.

## Let the web app use your real project folders (console access)

By default, commands are confined to a dedicated workspace folder
(`~/DeepCerebra`). To use your pre-configured CLIs in your actual projects,
grant folders explicitly:

```bash
# grant one or more real folders (repeatable)
python -m dcc_bridge --gateway wss://deepcerebra.ai --token dcc_brg_xxxxx --host-dir "F:\MyProjects"

# or the whole machine (prints a red warning; prefer --host-dir)
python -m dcc_bridge --gateway wss://deepcerebra.ai --token dcc_brg_xxxxx --allow-any-dir
```

Then, in the web terminal, open the **Execution target** popover (laptop icon)
and choose **My computer** plus a working directory.

## Options

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--gateway` | `DCC_BRIDGE_GATEWAY` | — | Web app URL (`wss://deepcerebra.ai` / `wss://deepcerebra.io`) |
| `--token` | `DCC_BRIDGE_TOKEN` | — | One-time pairing token from the Local Machine page |
| `--workspace` | `DCC_BRIDGE_WORKSPACE` | `~/DeepCerebra` | Confined default folder |
| `--host-dir PATH` | `DCC_BRIDGE_HOST_DIRS` (`;` on Windows, `:` on Unix) | none | Grant a REAL folder (repeatable) |
| `--allow-any-dir` | `DCC_BRIDGE_ALLOW_ANY_DIR=true` | off | Full host access (use with care) |
| `--no-exec` | `DCC_BRIDGE_ALLOW_EXEC=false` | exec on | Inference-only device (no commands) |
| `--ollama-host` | `OLLAMA_HOST` | `http://localhost:11434` | Ollama server |
| `--lmstudio-host` | `LMSTUDIO_HOST` | `http://localhost:1234` | LM Studio server |

## Security model (layered, all opt-in)

- A device only joins after **you** create a pairing token in the web app;
  tokens are Argon2-hashed at rest and revocable anytime from the Local Machine
  page.
- Without host flags, every command is **jailed to the workspace folder**; path
  traversal is refused.
- Host access never exceeds the folders **you** listed on the command line;
  file syncs from the browser can never write into host folders.
- `--no-exec` makes a device inference-only; the web app has its own "Local
  execution" kill switch.
- Caps: 32 MB / 4000 files per sync, 1 MB output per stream, 900 s per command.

## Source

This repository is a **standalone mirror** of the `connector/` folder of the
main DeepCerebra repo, published separately so end users can install just the
connector. Full documentation: `docs/DEEPCEREBRA_CONSOLE_BRIDGE.md` in the main
repo.
