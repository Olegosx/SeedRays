[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0026-configuration-file.md)

# ADR-0026: A Configuration File as the Deployment Layer

**Status:** accepted; partially revises [ADR-0016](0016-config-layers.md) — the deployment
layer moves from environment variables into a file, while the split between the two
configuration layers stays as it was

## Context

[ADR-0016](0016-config-layers.md) split the gateway's configuration in two: the deployment
layer, which the process needs before it can open any database, and the registry `settings`
table, which the operator manages while the gateway runs. That split is sound and is not in
question here.

The medium of the lower layer is. ADR-0016 weighed exactly two alternatives — "everything in
environment variables" and "everything in the database" — and a configuration file was never
among them. The decision to use environment variables therefore rests on an argument against
the database, not on a comparison with the option this record adopts.

Meanwhile ADR-0016 itself places the future PostgreSQL connection string in that layer, and
that string carries a password. Verified on a live Linux system: unit files in
`/etc/systemd/system` are world-readable by default (`-rw-r--r-- root root`), and
`systemctl show <unit> -p Environment` prints a unit's environment to an unprivileged user.
A password in `Environment=` is therefore readable by anyone with a shell on the server.

## Decision

- **The deployment layer is a TOML file, `seedrays.toml`.** It carries the data directory,
  the API bind address, the static frontend directory and, once PostgreSQL arrives, the
  registry connection string.
- **Environment variables are no longer a configuration source at all.** `SEEDRAYS_DATA_DIR`,
  `SEEDRAYS_BIND` and `SEEDRAYS_FRONTEND_DIR` are removed rather than kept as overrides: a
  mechanism that does not exist cannot leak a password by accident.
- **Two search locations, in order**: `seedrays.toml` in the repository checkout, then
  `/etc/seedrays/seedrays.toml`. The first one found wins, and **the file that was actually
  read is logged at startup** — with two locations the operator must never have to guess
  which one won.
- **`--config <path>` overrides the search** for the commands that touch gateway data. It is
  a command-line argument, not an environment variable: it does not appear in
  `systemctl show`, is not inherited by child processes, and carries a path rather than a
  secret. It is what lets one machine run several gateways with different data directories.
- **TOML, not JSON**, because the file is edited by hand on a server and JSON cannot carry
  comments — the explanation of every setting would have to live somewhere else, which
  defeats the point of having one obvious place. `tomllib` is part of the standard library
  from Python 3.11, so the format costs no dependency.
- **A relative path resolves against the directory holding the configuration file**, not
  against the working directory, so one value cannot mean different places depending on where
  the process was started. Servers use absolute paths.
- **An unknown key is an error naming the key**, not a silently ignored line — the same
  strictness the operator panel already applies to an unknown setting.
- Only the data directory is required. The bind address defaults to `127.0.0.1:8080`; the
  frontend directory defaults to the `frontend/` directory of the checkout the package runs
  from.

## Alternatives Considered

- **Environment variables (the previous decision):** rejected — a password in a unit's
  `Environment=` is readable by any user of the server (verified, see the context), and the
  configuration of the gateway ends up inside a process-supervision file instead of a place
  of its own. Their one real advantage, ergonomics in containers, is not worth that.
- **Keeping the variables as overrides on top of the file:** rejected by the owner — one
  source of truth, and no mechanism through which a connection string could reach the
  environment by accident.
- **JSON:** rejected — no comments; a configuration file edited by hand during an incident
  needs them. JSON5/JSONC would fix that at the price of a dependency.
- **INI via `configparser`:** workable and familiar to administrators, but every value is a
  string and there is no nesting — thin ground for a connection string and for whatever the
  file grows into.
- **A single location instead of two:** rejected — a checkout-local file keeps development
  self-contained, while `/etc/seedrays/seedrays.toml` is where a server administrator looks
  first. The cost is the risk below.

## Consequences

- No deployment exists yet, so nothing has to be migrated; this is a breaking change with no
  one to break.
- The deployment document gets simpler: one commented file instead of a table of variables
  and `Environment=` lines in the systemd unit.
- **A stray `seedrays.toml` left in the code directory on a server silently outranks
  `/etc`.** Mitigations: only `seedrays.example.toml` is committed, the real file is in
  `.gitignore`, and the startup log names the file that was read.
- The file must be readable by the service user only (`chmod 600`) once it carries the
  database password.
- Reading the configuration stays in one place at the process boundary: `cli.py` resolves it
  and passes plain values down. Nothing below the entry point learns that configuration files
  exist, which is the thin-wrapper principle of [ADR-0001](0001-library-first-core.md).

## Related

- [ADR-0016: Configuration Layers](0016-config-layers.md)
- [ADR-0001: Library-First Core With Thin Wrappers](0001-library-first-core.md)
- [Deployment](../../40-operations/deployment.md)
- [Orchestrator](../components/orchestrator.md)
