---
title: Skills
description: Units of know-how an agent carries — frontmatter, a body rendered over a config, and resources read on demand.
---

# Skills

Use these capability snippets inside your application’s [native runtime](native-runtime.md).
Installation is through [PyFly Agentic extras](getting-started/installation.md).

A **skill** is a unit of know-how in the Agent Skills shape: YAML frontmatter, a markdown body,
and resources. The three go to three different places, and that is the point:

| Part | Where it goes | Why |
|---|---|---|
| frontmatter (`key`, `name`, `description`, `when_to_use`, `version`, `requires`, `parameters_schema`, `examples`, the resource index) | your store, your editor, the prompt's heading | metadata a host versions and shows |
| body (markdown, bounded Jinja-style config interpolation) | the system prompt, once per skill, rendered | what the model follows |
| resources (reference documents, templates) | **not** the prompt — fetched through the `read_skill` tool on the turn they are needed | an agent with twelve skills carries twelve bodies and no appendices |

That last row is progressive disclosure, and it is why a skill is not just a longer prompt.

## Author one

`SKILL.md`:

```markdown
---
name: Local GAAP close
description: Close the books under local GAAP with the firm's checklist.
when_to_use: When a client asks for a month-end or year-end close under local GAAP.
version: 3
requires:
  tools: [dw_workspace_write]
parameters_schema:
  type: object
  required: [entity]
  properties:
    entity: {type: string, description: The legal entity being closed.}
    fiscal_year_end: {type: string, enum: ["12-31", "06-30"], default: "12-31"}
  additionalProperties: false
resources:
  checklist.md: A step-by-step close checklist.
  templates/memo.md: {kind: template, description: The close memo the partner signs.}
---
# Closing under local GAAP

You are closing **{{ entity }}** with a fiscal year ending {{ fiscal_year_end }}.
Read `checklist.md` before starting.
```

```python
from pyfly.agentic.skills import load_skill, parse_skill

skill = load_skill("skills/x_local_gaap")          # SKILL.md + the declared resources, loaded lazily
skill = parse_skill(text, key="x_local_gaap")       # text only; attach resources with with_resources()
```

- The key matches `^[a-z][a-z0-9_]*$`; it comes from `key=` or the frontmatter's `key`.
- Resource paths are relative and stay inside the skill: `..` and absolute paths are refused, and
  `load_skill` exposes only the resources the frontmatter declares — the directory is not a file share.
- `load_skill` requires POSIX descriptor-relative file operations. It rejects symlinks in the selected
  root, skill filename and resource paths, non-regular files, and root replacement before lazy reads.
  Every path component is opened without following links; validation and reading use the same descriptor.
- Frontmatter keys the framework does not read are kept under `skill.metadata`. Duplicate YAML keys,
  aliases and excessive nesting are refused. Metadata, config and schemas must contain JSON values.

## Bind and render

```python
bound = skill.with_config({"entity": "Acme SL"})
bound.render()                      # the body with the config and schema defaults substituted
skill.validate_config({"entity": "Acme SL"})        # every problem at once, [] when valid
skill.unmet(tools={"dw_workspace_write"}, connectors=set(), skills=set())   # what the host does not provide
skill.digest()                      # SHA-256 over everything the model can see
```

`parameters_schema` recursively supports `type` (string, number, integer, boolean, array, object,
null), `required`, `properties`, `items`, `enum`, `minimum`/`maximum`, `minLength`/`maxLength`,
`minItems`/`maxItems`, `default`, and boolean `additionalProperties`. `title` and `description` are
annotations. Other keywords, including `$ref`, patterns and composition, raise `SkillParseError`
at construction; this is an explicit subset, not a full JSON Schema implementation. Defaults are
validated at construction and applied recursively before config validation. A bad config raises
`SkillConfigError` on render. `validate_config()` returns at most 128 problems with nested paths.
Nullable fields accept a two-element type array containing one supported type and `null`, in
either order (for example `type: [string, "null"]`). General type unions are refused. Nested
objects and array items retain validation when non-null; enum values distinguish booleans from
numbers. Required property names must be unique and declared in `properties`.
Default expansion consumes one shared depth/node/byte budget during traversal, so arrays of
objects cannot multiply nested defaults into an oversized intermediate result before validation.

The body accepts **interpolation only**: `{{ entity }}`, dictionary lookup `{{ client.name }}` or
`{{ client["name"] }}`, and nonnegative array indexes `{{ rows[0] }}`. Dot lookup reads dictionary
keys, never Python attributes. The supported filters are `default(value[, boolean])`, `upper`,
`lower`, `title`, `trim` and `join("separator")`. Filter arguments must be literals. Missing values
fail unless handled with `default`. Loops, blocks, includes, calls, arithmetic, arbitrary filters,
dynamic indexes and Python attribute traversal are refused. The interpreter parses Jinja syntax
but never compiles or executes a Jinja template. Markdown is preserved without HTML escaping;
hosts displaying it in a web page must escape or sanitize at that rendering boundary.

Config, schema and metadata are copied into deeply immutable mappings and tuples. Non-JSON values,
non-string object keys and nonfinite numbers are refused. `digest()` uses canonical JSON with
sorted keys and includes the bound config and resource texts. Canonical serialization changes
digests from earlier implementations: republish/version existing snapshots explicitly rather than
treating an old digest as proof of identity.

| Boundary | Limit |
|---|---|
| Body, rendered body, each ordinary text/config string | 65,536 characters |
| Complete assembled skills prompt | 65,536 characters |
| Config, schema or metadata object | 16 levels, 4,096 value nodes, 262,144 canonical JSON bytes |
| JSON integer | 4,096 bits |
| Template AST | 1,024 nodes, depth 32 |
| Selected skills; resources/examples per skill | 128 each |
| Requirements per category | 128 names, 256 characters each |
| `SKILL.md` or one resource | 1,048,576 UTF-8 bytes |
| Resource text included by one `digest()` | 8 MiB total |
| Resource path or custom skill filename | 4,096 characters, 16 components (at most 17 open descriptors per read) |
| YAML frontmatter | 8,192 events, nesting depth 16, no aliases |

Custom resource loaders are trusted application callbacks, not skill-authored executable code;
the framework checks their returned text size but cannot bound callback execution. Filesystem
resources remain lazy and may change before their first read. Publication hosts should materialize
approved resource bytes as inline `SkillResource(content=...)`, store a digest and retain that
exact skill value for execution. These primitives do not provide approval storage or a publication
transaction.

## Put it on an agent

```python
from pyfly.agentic.agents import Agent
from pyfly.agentic.skills import build_read_skill_tool, render_skills_section

skills = [skill.with_config({"entity": "Acme SL"})]
agent = Agent(
    "closer",
    model=model,
    instructions="You are a careful accountant.\n\n" + render_skills_section(skills),
    tools=[build_read_skill_tool(skills)],
)
```

`render_skills_section` produces a `## Skills` section: one `###` per skill with its key, version,
when-to-use and rendered body, followed by the resource list as the exact
`read_skill(key="…", path="…")` calls that fetch them. `read_skill` is a plain `AgentTool` — no
approval, no side effects, reads only what the skills declared, answers a wrong key or path with a
`ModelRetry` naming what exists, lists a skill's resources when `path` is empty, and cuts a long
resource at `max_chars` saying so. Pass `listeners=` so a host's ledger sees these reads like any
other call.

Both assembly functions reject duplicate skill keys, including equal duplicates, so the prompt
cannot describe a different skill from the one the tool reads. Requirements describe prerequisites;
the host must check `unmet()` and authorize actual tools/connectors separately. A skill's text does
not grant permissions or secret access. Supply runtime secret references through native model/tool
dependencies, never skill config or instructions. Retrieved external text belongs in evidence/tool
results with source identifiers and server-enforced access checks; do not turn retrieved documents
into template source or privileged skill instructions.
