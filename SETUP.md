# Setting the pilot up again

How to rebuild the test environment on a fresh machine. The machine it last ran on (Ubuntu 25.10) was
cleared after the project was wrapped up on 2026-11-14, so everything needed is written down here.
What the arms are and what they found is in [FINDINGS.md](FINDINGS.md); the design is in [README.md](README.md).

## What it needs

| Part | Version last used | Why |
|---|---|---|
| Linux with `sudo` | Ubuntu 25.10 | bubblewrap and Docker need root to install |
| Claude Code CLI, logged in to the subscription | 2.1.323 | every model call goes through `claude -p`; no API keys |
| bubblewrap | 0.11.0 | cuts the model process off from the machine |
| Docker (`docker.io`) | 29.1.3 | runs the agent's commands and the task code, without network |
| Python | 3.13 (any 3.11+) | the harness |
| uv | any | creates the virtual environment, runs the one-off data fetch |
| Disk | about 60 GB free | the ten SWE-bench images are 4 to 11 GB each |

## Steps

### 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap docker.io git
```

On Ubuntu 24.04 and later, AppArmor can block unprivileged user namespaces, which bubblewrap needs. If
`bwrap --unshare-all --share-net --ro-bind /usr /usr true` fails with "Permission denied", allow it for
bwrap (or set `kernel.apparmor_restrict_unprivileged_userns=0`) before going on.

### 2. Docker without a password prompt

The harness calls `sudo -n docker ...`, so the user running it needs passwordless sudo for Docker, and
nothing in the harness asks for more. (The last machine already gave its user passwordless sudo for
everything, so no rule was added there.) On a new machine, add a rule like this with `sudo visudo -f /etc/sudoers.d/hope-pilot`
(replace `admin` with the user):

```
admin ALL=(root) NOPASSWD: /usr/bin/docker
```

Do not add the user to the `docker` group instead: that hands root to anything the user runs, including
a misbehaving model process. The model process never gets sudo; it runs in bubblewrap with no tools of its own.

### 3. Claude Code

Install the CLI and log in once, interactively, with the subscription account:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude            # log in, then exit
```

The harness mounts `~/.claude/.credentials.json` read-only into the sandbox, and nothing else from the home
directory. Check which models the account serves (for example `claude-sonnet-4-5-20250929`); records store
the model that actually answered in `served`.

### 4. The repository and the Python environment

```bash
git clone git@github.com:Benogi/hope-pilot.git      # private repo; needs a key with access
cd hope-pilot
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest -q                       # 130 passed, 1 skipped
```

### 5. Benchmark data (only for the ImpossibleBench arms)

`data/` is ignored by git. Fetch it once:

```bash
uv run --no-project --with datasets python impossible_swe.py fetch
uv run --no-project --with datasets python impossible_lcb.py fetch
```

### 6. Docker images

Build the minisql image once; the rest is pulled on demand. The SWE arm pulls each `swebench/sweb.eval.x86_64.<task>` image the first time a
task runs, and the long task and chess use `hp-minisql:1` (python:3.12-slim with pytest), built once with `long_task.py build`.

### 7. Check everything before any model run

Build the image first: `.venv/bin/python long_task.py build`. Then run the selfchecks.

None of these call a model:

```bash
.venv/bin/python impossible_swe.py selfcheck
.venv/bin/python long_task.py selfcheck
.venv/bin/python chess_task.py selfcheck
.venv/bin/python chess_gift.py selfcheck
.venv/bin/python chess_lobby.py selfcheck
```

Each ends with `selfcheck passed`. The chess and SWE selfchecks also confirm the containers have no network.

## Running the arms

Each `run` writes one JSON line per run and skips runs whose id is already in the output file, so an
interrupted batch can be restarted with the same command. `report` prints the tables used in FINDINGS.md.

| Arm | Run | Report |
|---|---|---|
| Impossible-SWEbench | `impossible_swe.py run --models claude-sonnet-4-5-20250929 --splits conflicting --conditions A_control C_ancestor --scenarios base --limit 10 --out results_swe.jsonl` | `impossible_swe.py report results_swe.jsonl` |
| Long task (minisql) | `long_task.py build`, then `long_task.py run --models sonnet --out results_long.jsonl` | `long_task.py report ...` |
| Chess, bot cheats against the player | `chess_task.py run --models sonnet opus haiku --reps 5 --out results_chess.jsonl` | `chess_task.py report ...` |
| Chess, bot cheats for the player and accuses it | `chess_gift.py run --models sonnet opus haiku --reps 5 --out results_gift.jsonl` | `chess_gift.py report ...` |
| Chess with a Fable bot voice and a lobby | `chess_lobby.py run --players sonnet opus haiku --bot-characters plain brother --reps 5 --out results_lobby.jsonl` | `chess_lobby.py report ...` |

`--help` on each command lists the options (conditions, scenarios, reps, workers). The single-turn and fleet
arms are described in README.md.

Usage limits are the constraint, not money. A lobby table uses one Fable session for the bot's voice, and
five tables in a row used up the Fable allowance on 2026-11-14. Run lobby tables a few at a time.

## Isolation, in short

- **Model process:** bubblewrap (`run_pilot.sandbox_prefix`). It sees `/usr`, the claude binary, the
  credentials file read-only and one empty working directory. Only the network is shared, so the CLI can
  reach the API. Built-in tools are off (`--tools ""`); the only tools are two MCP relays to the harness.
- **Task code:** Docker with `--network none`, all capabilities dropped and memory, CPU and process limits.
- **Harness:** the relays talk to the harness over 127.0.0.1 with a per-run token. In chess the bot runs in
  the harness, outside the container, so the agent can neither read nor change it.
- **Checks:** if `bwrap` is missing, a run is refused. If the CLI starts with any tool other than the two
  allowed, or leaves files in its working directory, the run is stopped with `IsolationError`.

## Taking it down again

```bash
sudo docker rm -f $(sudo docker ps -aq --filter ancestor=hp-minisql:1) 2>/dev/null
sudo docker rmi hp-minisql:1 python:3.12-slim $(sudo docker images 'swebench/*' -q)
sudo rm -f /etc/sudoers.d/hope-pilot     # if you added it in step 2
sudo apt-get purge -y docker.io bubblewrap && sudo apt-get autoremove -y
rm -rf hope-pilot
```

Remove `docker.io` and `bubblewrap` only if nothing else on the machine uses them.
