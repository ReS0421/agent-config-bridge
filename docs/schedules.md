# Host-managed Schedules

Agent Config Bridge Schedules are version-controlled recurring prompts executed
by Codex CLI or Claude Code CLI. They use one canonical definition across native
Windows and Linux while keeping the scheduler, authentication, product home,
and working tree local to each target.

They are not Codex Desktop scheduled tasks, Claude Desktop scheduled tasks,
Claude CLI `/loop` entries, or Claude Remote Routines. Bridge Schedules do not
appear in those product-native scheduler views.

## Catalog schema

Each lowercase kebab-case Schedule directory contains exactly two real files:

```text
catalog/schedules/daily-review/
├── schedule.toml
└── PROMPT.md
```

Example `schedule.toml`:

```toml
schema_version = 1
cron = "0 9 * * 1-5"
timezone = "Asia/Seoul"
working_directory = "workspace/my-project"
timeout_seconds = 1800
```

The fields are:

| Field | Required | Contract |
| --- | --- | --- |
| `schema_version` | Yes | Integer `1` |
| `cron` | Yes | Five-field numeric Vixie-style expression |
| `timezone` | Yes | IANA timezone such as `UTC` or `Asia/Seoul` |
| `working_directory` | Yes | Portable POSIX-style path relative to target `user_home` |
| `timeout_seconds` | No | Integer `1..86400`; default `1800` |

`PROMPT.md` must be non-empty UTF-8 text. Discovery normalizes CRLF and lone CR
line endings to LF so Git checkout settings do not change snapshot identity or
vendor input across operating systems. The bridge passes the normalized text
to the product CLI on standard input; it is never inserted into a shell command
or environment variable.

The cron parser accepts wildcards, numeric values, inclusive ranges,
wildcard/range steps, and comma-separated lists. It rejects names and
extensions such as `L`, `W`, and `?`. Sunday may be `0` or `7`. When both
day-of-month and day-of-week are restricted, Vixie cron OR semantics apply.

The working directory must exist when `apply` renders a target snapshot. It
must physically resolve beneath that target's `user_home`; absolute paths,
backslashes, `..`, Windows reserved names, links that escape the home, and
special filesystem nodes are rejected. Use `working_directory = "."` to run at
the target user home.

Every canonical Schedule is published for every enabled target that selects
`schedules`. If the same recurring work should run only once, select the
component on only one target instead of enabling it for both Codex and Claude.

## Target configuration

Select `schedules` globally or in a target override, and include the `cli`
surface:

```toml
[bridge]
catalog = "./catalog"
state_dir = "./.agentbridge"
link_mode = "auto"
components = ["skills", "settings", "schedules"]

[[targets]]
name = "local-codex"
product = "codex"
platform = "auto"
user_home = "~"
executable = "/home/alice/.local/bin/codex"
surfaces = ["cli", "desktop"]
enabled = true
```

`executable` is an optional per-target override for Plugin/Hook registration,
its marketplace preflight, and Scheduled Codex or Claude Code runs. Use an
absolute host-native path for the clearest cross-host config.
The loader also accepts a relative spelling beneath `user_home`, but normalizes
it to an absolute path before use. Registration requires a real file, executable
permission on Linux, and a native `.exe` or `.com` launcher on Windows.

For the heartbeat, an omitted `executable` makes `register` search the
registering process `PATH`, accepting only absolute entries and skipping the
current directory and all relative entries. It applies the same launcher
validation. The resulting absolute path is embedded in the heartbeat and its
ownership digest, so Scheduled runs do not rely on cron or Task Scheduler PATH
lookup. Plugin/Hook registration uses the same validated override. Without one,
those interactive commands preserve the normal bare product command and PATH
lookup.

Use a host-specific config file with native paths. The heartbeat records the
absolute config path, so a native Windows target must be registered from
Windows and a Linux/WSL target from that Linux environment. Keep `state_dir`
host-local even when all hosts share the same canonical catalog.

## Lifecycle

Review and publish Schedule content first:

```bash
agentbridge validate
agentbridge plan
agentbridge apply --yes
```

`apply` renders an immutable target-specific snapshot and publishes a stable
pointer below `state_dir`. After Schedule content, cadence, worktree, or target
paths change, run `plan` and `apply` again.

Then review and register the host heartbeat:

```bash
agentbridge register --target local-codex --yes
```

One heartbeat per target wakes once per minute and evaluates every Schedule in
the published snapshot:

- Linux: a marked block in the current user's crontab;
- Windows: `AgentConfigBridge-Heartbeat-<target>` in Task Scheduler.

The registration stores an ownership digest. The bridge refuses to adopt,
replace, or remove an unrecorded or modified entry merely because its name or
marker resembles a bridge heartbeat.

Scheduler mutation and ownership-state persistence are separate operations. If
the process crashes after changing crontab or Task Scheduler but before writing
the target state, the next registration fails closed instead of adopting that
entry. Inspect and reconcile the external scheduler explicitly; alpha releases
do not provide automatic rollback for this window.

Run one published Schedule immediately without changing its cadence or
last-processed minute:

```bash
agentbridge schedule run \
  --config ./agentbridge.toml \
  --target local-codex \
  --name daily-review
```

`schedule run` is an immediate execution command, not a dry run, and has no
separate `--yes` prompt. It still requires a published snapshot and a matching
local target platform. It uses the same per-Schedule run lock as heartbeat
execution, so it skips rather than overlaps an already active run of that name.

The heartbeat itself invokes the due-check command:

```bash
agentbridge schedule tick \
  --config /absolute/path/to/agentbridge.toml \
  --target local-codex \
  --vendor-executable /absolute/path/to/codex
```

Normally users do not call `tick` directly. The registered heartbeat supplies
the internal `--vendor-executable` argument after validation; manual
`schedule run` resolves the configured override or current process `PATH`
itself.

## Runtime behavior

Each tick:

1. reads the current immutable target snapshot;
2. takes a short non-blocking target minute-claim lock;
3. skips a snapshot/minute already processed;
4. records the current snapshot/minute before starting work;
5. evaluates cron expressions in their declared IANA timezones;
6. releases the claim lock before vendor work;
7. runs due entries in catalog order, holding a separate lock for each
   target/Schedule name while its vendor process is active.

Duplicate claims for the same snapshot/minute are skipped. Heartbeat processes
from different minutes may overlap, so a long Schedule does not make subsequent
minutes disappear. If that same Schedule name is still running, its new
recurrence is skipped rather than queued; other due Schedules can continue.
Missed minutes are not replayed, and a failed attempt is not retried
automatically in the same minute.

Product execution uses fixed argument vectors:

```text
codex exec --ephemeral -C <working-directory> -
claude --print --no-session-persistence
```

The target's `CODEX_HOME` is set locally. Claude sets `CLAUDE_CONFIG_DIR` for a
custom home and removes an inherited value for its default profile. No
permission-bypass option is added. Each run starts a fresh CLI process and does
not inherit a Desktop thread, Claude CLI session, or previous Schedule context.

The heartbeat stores absolute paths for both `agentbridge` and the validated
product CLI. Changing either path requires another `register` so its owned
heartbeat and digest can be updated.

The Windows task uses `MultipleInstancesPolicy=Parallel` and
`ExecutionTimeLimit=PT0S`. Task Scheduler may start the next minute heartbeat
while earlier vendor work continues, and it does not impose a one-minute task
limit. Per-Schedule locks prevent duplicate overlap and `timeout_seconds` still
bounds each vendor subprocess. The task uses the current user's interactive
token and least privilege, so the relevant user session and local product
authentication must be available.

On POSIX, Schedule snapshots, pointers, minute markers, and lock files use mode
`0600`, while bridge-managed Schedule state/runtime directories use `0700`.
Windows mode bits do not enforce a private DACL; use an ACL-protected
user-private `state_dir` and verify inherited permissions separately.

## Deselecting or moving a target

To remove Schedules while retaining a target:

1. remove `schedules` from that target's `components`;
2. run `agentbridge plan` and `agentbridge apply --yes` to remove its published
   pointer;
3. run `agentbridge register --target <name> --yes` to remove its owned
   heartbeat.

Before renaming, moving, disabling, or deleting a target, keep its old identity,
set `components = []`, then run both `apply` and `register`. This also reconciles
the other component classes and avoids orphaned ownership state.

If the repository, config file, installed `agentbridge` executable, or
`state_dir` moves, rerun `apply` and `register` from the target host so the
snapshot and absolute heartbeat command can be reconciled.

## Native scheduler boundaries

- [Codex scheduled tasks](https://learn.chatgpt.com/docs/automations) are
  managed through ChatGPT web or Codex Desktop; Codex CLI does not expose a
  public equivalent import/management contract used by this bridge.
- [Claude Code CLI scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks)
  are session-scoped, and recurring `/loop` tasks expire after seven days.
- [Claude Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks)
  expose the local prompt file, but cadence, folder, model, and enabled state
  remain Desktop-managed.
- [Claude Remote Routines](https://code.claude.com/docs/en/web-scheduled-tasks)
  are account-owned cloud resources and already follow the account across
  machines.

The bridge does not copy private databases, automate those UIs, or claim a host
Schedule is visible or synchronized there. A future native adapter requires a
stable public lifecycle contract first.

## Unattended-execution safety

Treat `PROMPT.md` as executable supply-chain input. Review it like a Hook or
script, keep secrets out of the catalog, use the narrowest product permissions,
and choose a finite timeout. A host-scheduled process may not have a person
available for an approval prompt.

The bridge does not persist captured vendor output as an execution log. Host
schedulers and product CLIs may retain their own metadata or logs. If a workflow
needs a durable result, make that output an explicit, narrowly scoped part of
the reviewed prompt and product permission model.
