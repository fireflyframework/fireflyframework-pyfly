# Data-layer benchmark baseline

The numbers the ORM unit-of-work redesign is measured against. They were taken on the data layer as
it was before the redesign: `src/pyfly/data` is byte-identical to `02b792f` (v26.09.07). The branch
had only added the test matrix, the benchmark harness and the dependency floor (SQLAlchemy 2.0.54
instead of 2.0.49; the control run below shows that the version does not move these numbers).

- **Date:** 2026-09-25, 07:19 PDT
- **Code:** branch `fix/orm-unit-of-work` at `28a4afe` (product code as `02b792f`), SQLAlchemy 2.0.54,
  Python 3.12.13. The `derived` table was re-measured at `84ba2bf`, product code unchanged (see its
  section).
- **Machine:** Apple M5 Pro, 64 GiB, macOS 26.6.2. The database servers ran in Docker through colima
  0.10.3 (a 4 vCPU / 8 GiB Linux VM, aarch64), next to other running containers on the same VM. The
  client connected over the VM's forwarded localhost port, with no TLS.
- **Servers:** SQLite 3.53.1 (aiosqlite, file database), PostgreSQL 17.11 (`postgres:17-alpine`,
  asyncpg), MySQL 8.4.11 (`mysql:8`, asyncmy), MariaDB 11.8.9 (`mariadb:11`, asyncmy), all with the
  framework's default pool (no pre-ping).

## Commands

Run from the repository root, one backend at a time:

```bash
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true
uv run python benchmarks/data/run.py --backend sqlite-file --json baseline-sqlite-file.json
uv run python benchmarks/data/run.py --backend pg --json baseline-pg.json
uv run python benchmarks/data/run.py --backend mysql --json baseline-mysql.json
uv run python benchmarks/data/run.py --backend mariadb --json baseline-mariadb.json
```

Each server backend starts its container through testcontainers and removes it afterwards. Every
scenario runs on a fresh database. Latencies are wall-clock medians on one event loop; compare them only
with runs on the same machine. Statement counts are exact and machine-independent.

## FINDINGS p6 and p7, reproduced

The audit's `proofs.py` p6 and p7 (FINDINGS.md) ran locally on 2026-09-24, against PostgreSQL 17 in a
local container and against SQLite. The harness reproduces them:

| Measure | FINDINGS, PostgreSQL | Harness, PostgreSQL | FINDINGS, SQLite | Harness, SQLite |
| --- | ---: | ---: | ---: | ---: |
| p6 first unit of work (opens the connection) | 2.39 ms | 1.68 ms | 0.77 ms | 0.63 ms |
| p6 pooled unit of work, median of 200 | 1.079 ms | 1.019 ms | 0.301 ms | 0.301 ms |
| p6 pooled unit of work, p95 | 1.330 ms | 1.276 ms | 0.386 ms | 0.363 ms |
| p6 distinct server connections for 201 units | 1 | 1 | n/a | n/a |
| p6 fresh connection every time, median | 12.16 ms | 11.26 ms | 0.46 ms | 0.45 ms |
| p7 `save_all(100)` statements | `{'INSERT': 1, 'SELECT': 100}` | same | `{'INSERT': 100, 'SELECT': 100}` | same |
| p7 `save(1)` statements | `['INSERT', 'SELECT']` | same | `['INSERT', 'SELECT']` | same |

Control, with SQLAlchemy 2.0.49 (the audit's version) installed in place of 2.0.54, same commands with
`--scenario p6 p7`: PostgreSQL first 1.96 ms, pooled median 1.017 ms, p95 1.140 ms, 1 connection, fresh
11.85 ms; SQLite first 0.74 ms, pooled median 0.298 ms, p95 0.342 ms, fresh 0.44 ms; p7 identical.

## Baseline by backend

### Unit of work and pool (`p6`, `tx`)

`p6` is a raw unit of work on the `async_sessionmaker` bean. `tx` is the framework's unit of work: a
`@transactional` service method that calls `repository.count()`.

| Measure | SQLite file | PostgreSQL | MySQL | MariaDB |
| --- | ---: | ---: | ---: | ---: |
| p6 first unit of work | 0.632 ms | 1.684 ms | 1.856 ms | 1.749 ms |
| p6 pooled, median | 0.301 ms | 1.019 ms | 1.077 ms | 1.042 ms |
| p6 pooled, p95 | 0.363 ms | 1.276 ms | 1.215 ms | 1.125 ms |
| p6 distinct server connections (201 units) | n/a | 1 | 1 | 1 |
| p6 fresh connection, median | 0.45 ms | 11.26 ms | 2.85 ms | 2.34 ms |
| tx first call | 1.134 ms | 2.394 ms | 2.338 ms | 2.168 ms |
| tx median | 0.351 ms | 1.054 ms | 1.095 ms | 1.328 ms |
| tx p95 | 0.440 ms | 1.179 ms | 1.327 ms | 2.908 ms |
| tx statements per call | `{'SELECT': 1}`, 1 commit | same | same | same |

### Repository read outside a transaction (`read`)

`count()` called from a service method without `@transactional`.

| Measure | SQLite file | PostgreSQL | MySQL | MariaDB |
| --- | ---: | ---: | ---: | ---: |
| median | 0.172 ms | 0.378 ms | 0.398 ms | 0.384 ms |
| p95 | 0.223 ms | 0.530 ms | 0.487 ms | 0.503 ms |
| per call | `{'SELECT': 1}`, 0 commits, 0 rollbacks | same | same | same |

These numbers are fast because they are wrong. The read runs on the repository's injected session,
whose transaction never ends (F3/F4): no commit and no rollback, and one pooled connection stays
checked out for the life of the application. At the end of the scenario SQLAlchemy warns that the
garbage collector had to reclaim that connection. The redesign replaces this with a read auto unit
that returns its connection. The spec's target is one round trip on PostgreSQL with AUTOCOMMIT reads,
measured at about 0.45 ms, against about 0.94 ms for a short BEGIN/SELECT/ROLLBACK transaction.

### Writes (`p7`, `save`)

| Measure | SQLite file | PostgreSQL | MySQL | MariaDB |
| --- | --- | --- | --- | --- |
| `save_all(100)` statements | `{'INSERT': 100, 'SELECT': 100}` | `{'INSERT': 1, 'SELECT': 100}` | `{'INSERT': 100, 'SELECT': 100}` | `{'INSERT': 1, 'SELECT': 100}` |
| `save(1)` statements | `['INSERT', 'SELECT']` | same | same | same |
| `save(1)` in `@transactional`, median | 0.791 ms | 1.680 ms | 1.814 ms | 1.851 ms |
| `save_all(100)` in `@transactional`, median | 32.54 ms | 49.53 ms | 70.75 ms | 39.76 ms |

The SELECTs are the `refresh()` after each flushed entity (F11). PostgreSQL and MariaDB batch the
INSERT with RETURNING. SQLite and MySQL send one INSERT per row for this autoincrement key.

### `exists` (5,000 rows, all matching the derived predicate)

| Measure | SQLite file | PostgreSQL | MySQL | MariaDB |
| --- | ---: | ---: | ---: | ---: |
| `exists_by_id`, median | 0.353 ms | 1.120 ms | 1.176 ms | 1.002 ms |
| derived `exists_by_name`, median | 0.440 ms | 1.306 ms | 1.607 ms | 1.377 ms |

The SQL is the same on every backend, apart from the placeholder. `exists_by_id` loads the whole row
(`SELECT audit_item.id AS audit_item_id, audit_item.name AS audit_item_name FROM audit_item WHERE
audit_item.id = ?`). The derived `exists_by_name` counts every match (`SELECT count(*) AS count_1 FROM
audit_item WHERE audit_item.name = ?`). Neither statement has `LIMIT 1` or `EXISTS`.

### Derived query CPU (`derived`, 2,000 interleaved calls)

CPU is the event-loop thread's CPU time per call. It covers building, compiling and executing the
statement in Python, and excludes the wait for the database. The derived finder runs through the
repository inside a `@transactional` method. The two hand-written twins run on a session the harness
opens once from the `async_sessionmaker` bean, so the scenario uses no repository internals and
measures the same thing after the redesign.

This table was re-measured at `84ba2bf` (product code still as `02b792f`), 2026-09-25 07:45 PDT, on
the same machine and servers, with `--scenario derived` on each backend. That commit moved the twins
off the repository's private `_session` and onto the harness's own session, which WP01 requires.

| Measure (per call) | SQLite file | PostgreSQL | MySQL | MariaDB |
| --- | ---: | ---: | ---: | ---: |
| derived `find_by_name`, CPU | 106.8 µs | 94.6 µs | 106.0 µs | 109.0 µs |
| same statement built by hand each call, CPU | 105.6 µs | 93.1 µs | 104.0 µs | 106.7 µs |
| same statement built once, CPU | 85.7 µs | 72.0 µs | 82.2 µs | 84.2 µs |
| derived CPU over the prebuilt statement | +21.1 µs | +22.6 µs | +23.8 µs | +24.8 µs |
| derived `find_by_name`, wall | 189.5 µs | 404.2 µs | 470.0 µs | 458.7 µs |

The first run, at `28a4afe`, had the twins on the repository's session. Its overheads were +22.6,
+22.9, +28.8 and +25.1 µs, and its derived CPU was 107.4, 98.0, 121.6 and 110.0 µs. The derived
finder's own code path did not change between the two runs, so the spread on it is run-to-run noise.
A third run, a minute before this table on the same code, gave 104.3, 99.4, 107.4 and 107.5 µs.
Compare CPU figures over several runs, not one: a single MySQL run was 15 µs off the others.

A derived query costs what the hand-written equivalent costs, because it rebuilds its statement on
every call. Building the statement once saves about 21 to 25 µs of CPU per call.

### `stream_all` and IN lists (`stream`, `in_padding`)

| Measure | SQLite file | PostgreSQL | MySQL | MariaDB |
| --- | ---: | ---: | ---: | ---: |
| `stream_all()` over 5,000 rows, median | 20.94 ms | 21.87 ms | 22.50 ms | 21.77 ms |
| `find_all()` over 5,000 rows, median | 6.57 ms | 7.71 ms | 7.32 ms | 7.22 ms |
| `find_all_by_id(n)` for n = 1..64: distinct SQL texts | 64 | 64 | 64 | 64 |
| `find_all_by_id(n)`, median per call | 0.459 ms | 1.584 ms | 1.372 ms | 1.201 ms |

Every IN-list length produces its own SQL text. On asyncpg each text is one more prepared statement in
the connection's cache.

## Re-run after the datasource registry (WP02)

The datasource registry changes what every engine gets: `pool.recycle`, `application_name`, the
metered queue pool, a credential hook and, on SQLite, the connection setup (foreign keys, WAL,
`synchronous=NORMAL`) and SQLAlchemy's `BEGIN` recipe. It does not change the unit of work itself.

- **Date:** 2026-09-25, 10:05 PDT, same machine, servers and SQLAlchemy as the baseline.
- **Code:** branch `fix/orm-unit-of-work` at `63bec18`.
- **Commands:** `--scenario p6 tx read` three times on `sqlite-file` and on `pg`, and `--scenario save`
  three times on `sqlite-file`. The table gives the three runs.

| Measure | Baseline | After WP02 (three runs) |
| --- | ---: | ---: |
| SQLite file, p6 pooled median | 0.301 ms | 0.403 / 0.414 / 0.375 ms |
| SQLite file, tx median | 0.351 ms | 0.456 / 0.436 / 0.420 ms |
| SQLite file, tx statements per call | `{'SELECT': 1}`, 1 commit | `{'BEGIN': 1, 'SELECT': 1}`, 1 commit |
| SQLite file, read median | 0.172 ms | 0.173 / 0.166 / 0.162 ms |
| SQLite file, `save(1)` in `@transactional`, median | 0.791 ms | 0.687 / 0.725 / 0.679 ms |
| SQLite file, `save_all(100)` in `@transactional`, median | 32.54 ms | 32.57 / 33.01 / 31.41 ms |
| PostgreSQL, p6 pooled median | 1.019 ms | 1.103 / 1.053 / 0.934 ms |
| PostgreSQL, tx median | 1.054 ms | 1.221 / 1.096 / 1.078 ms |
| PostgreSQL, read median | 0.378 ms | 0.403 / 0.381 / 0.378 ms |

On SQLite, a unit of work that only reads costs about 0.1 ms more (+25 to 30 %). The engine now emits
a real `BEGIN` when the transaction starts. pysqlite used to defer it until the first write, so a
read-only unit ran no transaction at all, which is how a read-modify-write lost updates (C044, C116).
Each statement on aiosqlite is a hop to the driver's thread, and the statement counter now sees the
`BEGIN`. That is the price of real isolation. Writes got faster: WAL with `synchronous=NORMAL` costs one
fsync of the WAL per commit, so `save(1)` dropped by about 0.1 ms.

On PostgreSQL the numbers stay within the run-to-run noise of the shared VM; the first run is the
outlier. The registry adds no statement and no round trip to a pooled unit of work there.

## What the redesign is checked against

These are the acceptance points in the spec ("Acceptance and verification"):

- No regression on the pooled unit of work: `p6` and `tx`, medians and p95.
- A read auto unit at one round trip on PostgreSQL: `read` sends one statement and returns its
  connection.
- `save(1)` sends one statement, and `save_all(n)` sends no per-entity SELECT (`p7`).
- `exists` uses `LIMIT 1`: the `sql` field in the JSON output, and its `limit_1` flag.

A package that re-runs the harness gets every scenario it can run, even if one breaks. A scenario that
raises appears in the report as `{"error": "..."}`, its traceback goes to stderr, the `--json` file is
still written, and the exit status is 1.
