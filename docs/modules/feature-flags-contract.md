# Feature flag contract

PyFly and LaraFly evaluate feature flags identically. A flag document written for one framework evaluates to the
same value, variant and reason in the other, and a user lands in the same percentage bucket in a Python service and
in a PHP service. This page is the contract both frameworks implement. Each framework's test suite runs the same
conformance files, byte for byte, so a difference fails the build in either repository.

The contract builds on two OpenFeature standards rather than inventing a format:

- **The flagd flag-definition document** (`https://flagd.dev/schema/v0/flags.json`): flags, variants, a default
  variant, and targeting rules in JSON Logic with the `fractional`, `sem_ver`, `starts_with` and `ends_with`
  operators.
- **The flagd in-process evaluator semantics**, pinned to the evaluator suite of `open-feature/flagd-testbed`
  `v3.10.2` with the `fractional-v2` bucketing algorithm.

Firefly adds only a configuration shorthand and four reserved metadata keys. Both normalize to plain flagd before
anything is evaluated, so the evaluated document can always be served to, or read from, any flagd tooling.

## Flag documents

<!-- illustrative: a flag document an application writes; it is data, not code in this repository -->
```json
{
  "flags": {
    "new-checkout": {
      "state": "ENABLED",
      "variants": {"on": true, "off": false},
      "defaultVariant": "off",
      "targeting": {"if": [{"in": ["beta", {"var": "roles"}]}, "on", null]},
      "metadata": {"owner": "payments", "kind": "release", "expires": "2026-12-31"}
    }
  },
  "$evaluators": {"is-beta": {"in": ["beta", {"var": "roles"}]}},
  "metadata": {}
}
```

### Rules every definition must satisfy

| Rule | Message when broken |
|---|---|
| The key matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` | `invalid flag key` |
| The definition is a JSON object | `flag definition must be an object` |
| `state` is `ENABLED` or `DISABLED` | `state must be ENABLED or DISABLED` |
| `variants` is a non-empty object | `variants must be a non-empty object` |
| Every variant value has the same JSON type: boolean, string, number, or object/array (`null` is not a flag value) | `variants must share one type` |
| `defaultVariant` names a variant, or is absent or `null` | `defaultVariant is not a variant` |
| `targeting`, when present, is an object (`{}` means no targeting) | `targeting must be an object` |
| Flag `metadata` values are strings, numbers or booleans | `metadata values must be scalars` |
| `metadata.kind` is one of `release`, `experiment`, `ops`, `permission` | `kind must be one of release, experiment, ops, permission` |
| `metadata.expires` is a calendar date `YYYY-MM-DD` | `expires must be a YYYY-MM-DD date` |
| `metadata.owner` is a string | `owner must be a string` |
| `metadata.description` is a string | `description must be a string` |
| Flag `metadata` keys are strings (a YAML `on:` key is a boolean, not a string) | `metadata keys must be strings` |
| Metadata keys are not empty | `metadata keys must not be empty` |
| Numbers anywhere in a definition are finite (YAML accepts `.nan` and `.inf`; JSON does not) | `numbers must be finite` |
| A definition nests at most 256 levels anywhere (the flag object is level 1; each object or array inside adds one) | `definition nests too deeply` |

When a definition breaks more than one rule, the first rule in the table's order is reported, except that depth is
checked before finite numbers (a value too deep to walk is refused before its numbers are inspected).
| The document itself is an object | `document must be an object` (key `<document>`) |
| The document's `flags`, when present and not null, is an object | `flags must be an object` |
| The document's `$evaluators`, when present and not null, is an object whose names are strings and whose rules are objects | `$evaluators must be an object`, `evaluator names must be strings`, `targeting must be an object` (key `$evaluators`) |
| The document's `metadata`, when present and not null, is an object whose keys are strings and whose values are strings, numbers or booleans | `metadata must be an object`, `metadata keys must be strings`, `metadata values must be scalars` |

A YAML key that YAML reads as a date (an unquoted `2024-01-01:`) is not portable: PyFly refuses it (`metadata keys
must be strings`), while LaraFly's YAML parser turns it into a Unix-timestamp key before any check can see it. Quote
such keys. The same holds for any key at any depth inside a definition that YAML reads as something other than text
(`on:`, `null:`, `1.5:`, a date): PyFly refuses it, LaraFly's YAML 1.2 parser reads `on` and `null` as text. Quote keys.
Likewise an impossible calendar date written unquoted as a YAML value (`expires: 2025-02-30`): PyFly's
YAML parser refuses the file, while LaraFly's rolls the date over (`2025-03-02`) before validation sees it. Quote
dates in YAML; JSON cannot express the case.

An empty list (`[]`) where an object is expected — a flag's `targeting` or `metadata`, or a document section — counts
as an empty object (`{}`): PHP cannot tell the two apart in native configuration arrays. A non-empty list is still an
error.

Fields other than `state`, `variants`, `defaultVariant`, `targeting` and `metadata` are ignored: they are neither
an error nor evaluated, so a stray `description` beside `state` does not reject the document.

A `{"$ref": "name"}` that names no evaluator is not a load error. The document loads, and evaluating that flag
returns the caller's default with the error code `PARSE_ERROR`, as flagd does.

`$ref`s resolve on the parsed document, structurally and transitively: an evaluator may reference another evaluator,
whatever their names sort as, and strings inside an evaluator are kept exactly as written (a backslash stays a
backslash). A reference cycle, like a missing name, makes evaluating the flag that reaches it a `PARSE_ERROR`. So
does a flag whose targeting would expand to more than 10 000 JSON values (every object, array and scalar counts one,
and so does every `$ref` resolved along the way, so a chain of references to references stays bounded too):
references that fan out compound quickly, and the limit keeps a pathological document from exhausting memory. And so
does a flag whose expanded targeting nests deeper than 128 levels: the targeting object is level 1, each object or
array inside a container adds one, and a resolved `$ref` takes the place of its `{"$ref": …}` object without adding
a level. Both limits apply to one flag at a time; the rest of the document evaluates normally. References resolve only
inside a flag's `targeting`.

### Reserved metadata

| Key | Meaning |
|---|---|
| `description` | what the flag is for |
| `owner` | the team or person responsible for it |
| `kind` | `release` (ship dark, then turn on), `experiment` (variants under test), `ops` (kill switch or operational lever), `permission` (an entitlement) |
| `expires` | the last day the flag should exist |

A flag whose `expires` date is before today (UTC) is **expired**. It still evaluates normally. The admin page, the
`flags` actuator endpoint, the health details and one warning at startup list it as flag debt to remove.

### Shorthand

Configuration and test overrides accept a shorthand for the two most common flags:

| Written | Normalized definition |
|---|---|
| `true` | `{"state": "ENABLED", "variants": {"on": true, "off": false}, "defaultVariant": "on"}` |
| `false` | `{"state": "ENABLED", "variants": {"on": true, "off": false}, "defaultVariant": "off"}` |
| `"v2"` | `{"state": "ENABLED", "variants": {"v2": "v2"}, "defaultVariant": "v2"}` |

Numbers, arrays and objects have no shorthand; write the full definition.

## Evaluation

| Situation | Value | Variant | Reason | Error code |
|---|---|---|---|---|
| flag `DISABLED` | caller default | none | `DISABLED` | |
| no targeting | `defaultVariant` value | `defaultVariant` | `STATIC` | |
| targeting returns a variant name | that variant's value | that variant | `TARGETING_MATCH` | |
| targeting returns `null` | `defaultVariant` value | `defaultVariant` | `DEFAULT` | |
| no `defaultVariant` (absent, `null` or `""`) and nothing matched | caller default | none | `DEFAULT` | |
| unknown flag | caller default | none | `ERROR` | `FLAG_NOT_FOUND` |
| value type differs from the requested type (a boolean is never a number) | caller default | none | `ERROR` | `TYPE_MISMATCH` |
| invalid targeting or a missing `$ref` | caller default | none | `ERROR` | `PARSE_ERROR` |
| targeting returns a name that is not a variant | caller default | none | `ERROR` | `GENERAL` |

Targeting rules see the evaluation context attributes as variables, plus `targetingKey`, `$flagd.flagKey` (the flag
being evaluated) and `$flagd.timestamp` (Unix seconds).

### Percentage bucketing

`fractional` assigns a stable bucket:

1. `bucketBy` is the explicit first argument when it is an expression, otherwise `$flagd.flagKey` concatenated with
   `targetingKey`.
2. `hash` is MurmurHash3 x86 32-bit of the UTF-8 bytes of `bucketBy`, seed 0, read as an **unsigned** integer.
3. Weights are non-negative integers (a negative weight counts as 0); their total must not exceed 2³¹−1.
4. `bucket = (hash × totalWeight) >> 32`; the variant is the first whose cumulative weight exceeds `bucket`.
5. Without a targeting key and without an explicit expression the operator yields `null`, so the default variant
   applies.

<!-- illustrative: a targeting rule an application writes; it is data, not code in this repository -->
```json
{"fractional": [["control", 45], ["treatment", 45], ["holdout", 10]]}
```

## Evaluation context

Each framework builds an ambient context and merges the caller's explicit context over it; the caller wins.
Within an explicit JSON context, `targetingKey` accepts a non-empty string or an integer (converted to decimal
text). A boolean is not an integer. Null and an empty string supply no key; other JSON types are ignored with a
DEBUG diagnostic, preserving a lower-precedence key when present. `targetingKey` is removed from the ordinary
attributes. A separately supplied non-empty targeting-key argument takes precedence over the explicit context.
Language-specific API signatures still apply to that separate argument.

| Attribute | Value |
|---|---|
| `targetingKey` | the stable identifier of the authenticated principal; absent for anonymous traffic |
| `roles` | the principal's role names, without a `ROLE_` prefix |
| `tenant` | the tenant identifier, read from the principal attribute named by `context.tenant-attribute` (default `tenant`) |
| `application` | the application name |
| `profiles` | the active profiles |

Context values are JSON values, with one conversion: a date-time value (a Python `datetime`/`date`, a PHP
`DateTimeInterface`) is evaluated as its Unix epoch time in **milliseconds**, a number, in both frameworks, so a rule
such as `{">": [{"var": "signupAt"}, 1735689600000]}` decides the same everywhere and never depends on the host's time
zone. The number is computed exactly the same way in both: the whole number of microseconds since the epoch, divided
by 1 000 and correctly rounded to the nearest floating-point number; a date-time without a zone is read as UTC, and a date is midnight UTC.

Applications add attributes, such as a subscription `plan`, with context contributor beans that run after the
built-in ones. Anonymous traffic has no targeting key, so a percentage rollout gives it the default variant unless
the application contributes a stable anonymous identifier.

## Sources and precedence

Flags come from layered sources, lowest precedence first:

1. **config**: definitions inline in the application configuration (`feature-flags.flags`, `feature-flags.evaluators`)
2. **file**: a flagd document in JSON or YAML, reloaded when it changes
3. **http**: a flagd document polled from another service's sync endpoint
4. **store**: the writable layer behind the admin page, the actuator endpoint and the CLI
5. **test overrides**: test support only

The highest layer that defines a key supplies its **whole** definition; definitions are never merged field by
field. Shared evaluators merge by name with the same precedence, and document metadata merges by key. Every
composed flag records its `origin` and the lower layers it `overrides`. The test-only layer's public source name is
`test-overrides`, including composition origins and layer entries. Its change events use the same name. It is
excluded from the shared document served by the sync endpoint.

A source that fails keeps its last good document, and a document that fails validation is rejected as a whole. At
startup an invalid definition in the configuration or in the file stops the application with the flag key and the
reason; a remote or store failure never stops it. Each change to the effective set publishes a
`FeatureFlagsChanged` event naming the changed keys.

## The store

The store layer is two relational tables with the same logical schema in both frameworks, so two applications that
share a database share their runtime flag changes.

<!-- illustrative: the logical schema; each framework ships its own migration that creates these tables -->
```sql
CREATE TABLE firefly_feature_flags (
    flag_key    VARCHAR(128) NOT NULL PRIMARY KEY,
    definition  TEXT         NOT NULL,
    version     INTEGER      NOT NULL,
    updated_at  TIMESTAMP    NOT NULL,
    updated_by  VARCHAR(255) NULL
);

CREATE TABLE firefly_feature_flag_changes (
    id          BIGINT       NOT NULL PRIMARY KEY,
    flag_key    VARCHAR(128) NOT NULL,
    action      VARCHAR(16)  NOT NULL,
    definition  TEXT         NULL,
    previous    TEXT         NULL,
    actor       VARCHAR(255) NULL,
    changed_at  TIMESTAMP    NOT NULL
);

CREATE INDEX firefly_feature_flag_changes_key ON firefly_feature_flag_changes (flag_key, id);
```

The SQL above describes logical types. Flag keys compare exactly, including case, on every supported backend.
MySQL and MariaDB use a binary collation for flag keys and `LONGTEXT` for JSON payloads, so their ordinary
case-insensitive collations and 64 KiB `TEXT` limit do not change the contract. Timestamps store zoneless UTC with
microsecond precision: `DATETIME(6)` on MySQL/MariaDB, `TIMESTAMP(6) WITHOUT TIME ZONE` on PostgreSQL, and equivalent
UTC timestamp text on SQLite. A connection's session timezone must not reinterpret those stored UTC values.

- `definition` holds one flagd flag object as JSON. `version` starts at 1 and grows by one per update. Times are UTC.
  `id` is auto-incremented. `action` is `put` or `delete`; `definition` is the state after the change (null on
  delete) and `previous` the state before it (null on the first put).
- Every write updates `firefly_feature_flags` and appends one `firefly_feature_flag_changes` row in the same
  transaction.
- After a durable put or delete commits, the writer returns its committed change even if the local refresh is
  refused or fails, a change/update listener throws, or warning logging fails. Refresh and
  `FeatureFlagUpdated` publication are attempted independently after commit. A failed refresh retains the
  last good composition; observer failures are logged best-effort and never reported as a failed write.
- The store **revision** is the highest change `id` (0 when there is none). Readers poll it every
  `refresh-interval` and reload the rows only when it moved. When its local refresh succeeds, the process that
  writes sees its change at once.
- A write may carry `expectedVersion`. When the stored version differs, or a row exists where `0` was expected, the
  write is a `conflict` and changes nothing.
  Create-only writes (`expectedVersion: 0`) arbitrate absence with an atomic insert, without first creating an
  unnecessary read snapshot or missing-row lock. Two competing creates yield one stored definition and one audit
  entry, with a conflict for the loser unless the database aborts its parent transaction as described below.
  Without an expected version, a store-owned root operation retries a known
  row race for at most three attempts using fresh transactions; it never replays a caller-owned transaction.
  This does not promise recovery from arbitrary database errors.
- Native transaction aborts remain database transaction failures. In particular, MariaDB with snapshot isolation
  enabled can abort the entire caller transaction when its existing snapshot conflicts with a concurrent insert;
  a savepoint cannot preserve earlier caller writes after that engine-level abort. The caller must roll back and
  retry its complete unit of work. Neither an operation-level conflict exception nor a transaction counter proves
  that the parent is still usable. Native conformance tests pin both the final flag/audit state and preservation
  or loss of caller-owned marker rows under the configured backend isolation; no aborted parent may report a
  successful commit.
- Enabling, disabling or changing the default variant of a flag the store does not hold copies the current
  effective definition into the store with the change applied. That override shadows the lower layers until it is
  deleted, which reverts the flag to the next layer.

## The sync endpoint

A service serves its effective flag set; other services poll it as their `http` source.

- `GET` on the configured path (default `/feature-flags/flagd.json`) returns the composed document, every layer
  except test overrides, as flagd JSON with `ETag: "<sha256 of the body>"` and `Cache-Control: no-cache`.
- A request whose `If-None-Match` contains the current ETag (strong or weak, including a comma-separated list),
  or is `*`, gets `304 Not Modified`. Authentication is checked before the condition.
- A request must carry `Authorization: Bearer <server.token>`; a missing or wrong token gets `401`. Enabling the
  endpoint without a token stops startup unless `server.allow-anonymous` is true.
- The polling client sends `If-None-Match`, its bearer token and a timeout. A `304` keeps its document; any error
  keeps its last good document and shows in health. A `304` without a previously accepted revision is an error.
  When a valid response omits its ETag or sends an empty one, the client uses the quoted SHA-256 of its exact body
  bytes as the revision and sends it on the next conditional request.

## The `flags` actuator endpoint

Both frameworks answer with the same JSON.

`GET /actuator/flags` lists the provider, the enabled sources in precedence order, and every flag. Every timestamp
in the management JSON is UTC ISO-8601 with seconds and a `Z` suffix:

<!-- illustrative: the shape of a response; values depend on the application -->
```json
{
  "provider": {"name": "firefly", "status": "READY"},
  "writable": true,
  "writesEnabled": true,
  "sources": [
    {"name": "config", "enabled": true, "status": "UP", "flags": 2, "lastRefresh": "2026-10-01T09:30:00Z",
     "error": null, "revision": null}
  ],
  "flags": [
    {"key": "new-checkout", "state": "ENABLED", "type": "boolean", "variants": ["on", "off"],
     "defaultVariant": "off", "targeting": true, "origin": "store", "overrides": ["config"],
     "metadata": {"owner": "payments", "kind": "release", "expires": "2026-12-31"}, "expired": false, "version": 3}
  ]
}
```

`GET /actuator/flags/{key}` returns `key`, `definition`, `origin`, `layers` (each source's definition),
`version`, `expired` and `history` (the latest 50 changes, newest first: `id`, `action`, `actor`, `changedAt`).
An unknown key is a 404 with `{"error":"unknown-flag","message":"<text>"}` in both frameworks.

`POST /actuator/flags/{key}` takes an `action`:

| Action | Body | Needs writes |
|---|---|---|
| `evaluate` | `context` (object), `targetingKey` | no |
| `enable`, `disable` | `expectedVersion` (optional) | yes |
| `default-variant` | `variant`, `expectedVersion` (optional) | yes |
| `put` | `definition`, `expectedVersion` (optional) | yes |
| `delete` | `expectedVersion` (optional) | yes |

`evaluate` answers `key`, `value`, `variant`, `reason`, `errorCode` and `metadata`. The preview evaluates with the
body's `context` and `targetingKey` plus the process attributes `application` and `profiles`, never the caller's
own principal, and it records no metric and no exposure event. Evaluating or deleting a key that no layer defines
(for `delete`: that the store does not hold) is `unknown-flag`. A successful write answers
the key's `GET` body after the write, or `{"key": …, "deleted": true}` when a delete leaves no layer defining it.
If a successful `put` has no locally visible definition because refresh is deferred, fails, or is refused, it
answers `{"key": …, "refreshPending": true}` instead. This receipt does not claim a deletion or a completed
caller-owned transaction. When a last-good definition remains visible, the response is its current `GET` body;
the response alone does not guarantee that this process has adopted the latest store revision. Normal polling
recovers visibility. A post-write diagnostic failure must not turn an accepted write into a failed operation.
Writes need `management.writes` and a store.

Errors answer `{"error": "<code>", "message": "<text>"}`:

| Code | Meaning | LaraFly status |
|---|---|---|
| `writes-disabled` | `management.writes` is false | 403 |
| `not-writable` | no store is configured | 409 |
| `invalid-definition` | the definition breaks a rule above | 422 |
| `unknown-flag` | no layer defines the key | 404 |
| `unknown-variant` | the variant is not one of the flag's variants | 422 |
| `conflict` | `expectedVersion` did not match | 409 |
| `bad-request` | the body is malformed | 400 |

PyFly's actuator answers every POST management error body with 400, the contract of its write operations; GET
unknown selectors still answer 404. Malformed JSON, an empty body, or a non-object POST body answers 400 with
`bad-request` in both frameworks. The `error` code is the portable signal. The actor recorded for a write is the
authenticated principal's name, or `actuator`, `cli:<os-user>` or `admin` when there is none. This fallback comes
from the trusted calling channel: HTTP query parameters, headers and body fields cannot select the admin actor.

## Telemetry and events

- Counter `feature_flag_evaluations_total` with labels `flag`, `variant` (`none` when there is no variant) and
  `reason` (`ERROR` for a failed evaluation). Telemetry never changes an evaluation.
- `FeatureFlagsChanged(changedKeys, origin)` when the effective set changes; `origin` is the refreshed source's
  name, `startup` for the first composition, or `test-overrides`.
- `FeatureFlagUpdated(key, action, actor, previous, current)` after a committed store write.
- `FeatureFlagEvaluated(key, value, variant, reason, errorCode, targetingKey)` after every evaluation, only when
  `events.evaluations` is true. These are the exposure records an experiment's analysis needs.
  Exposure snapshots are best-effort: before copying, inspect at most 10 000 value occurrences. The root and every
  container or scalar (including null) each count one; object keys do not count; repeated references count again.
  Exactly 10 000 is allowed. If another occurrence remains, omit the exposure and log at DEBUG best-effort, leaving
  the evaluation result and its metric unchanged. This is only an exposure-snapshot limit, not a definition
  acceptance, stored-value, caller-default or evaluation limit. Cyclic runtime values exhaust this budget; no
  partially copied exposure is published.

## Conformance files

Both repositories carry the same files, and a test in each recomputes their SHA-256 manifest:

- `testbed/`: the flagd-testbed `v3.10.2` evaluator suite. Every scenario runs except those tagged
  `@fractional-v1`, the legacy bucketing.
- `firefly-vectors.json`: shorthand normalization, validation messages, layer composition, expiry, evaluation
  cases, and five bucketing tables of 301 targeting keys each, generated by the reference evaluator.
- `exposure-vectors.json`, `observer-vectors.json`, `override-vectors.json`, `context-vectors.json`,
  `management-vectors.json`, `management-transport-vectors.json`, `store-concurrency-vectors.json`, and `http-vectors.json`: shared boundary and
  lifecycle cases consumed by the corresponding framework adapters.
- `MANIFEST.sha256`: the SHA-256 of every payload file above.

To change the contract, change the files in both repositories in the same release and regenerate the manifest.
