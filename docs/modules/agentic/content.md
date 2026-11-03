# Content Processing Guide

Use these capability snippets inside your application’s [native runtime](native-runtime.md).
Installation is through [PyFly Agentic extras](getting-started/installation.md).

Copyright 2026 Firefly Software Foundation. Licensed under the Apache License 2.0.

The Content module provides utilities for splitting, chunking, and compressing large
inputs before they are sent to LLM agents. This is essential for processing documents,
images, and other artefacts that exceed a model's context window. It also includes a
binary normalisation subsystem (the `[agentic-binary]` extra) that turns raw uploaded files
(PDF, Office, images, archives, emails) into consumer-ready artifacts, and a content
sources layer for discovering files to ingest.

All chunking and compression symbols are canonically re-exported from the package root,
so the shortest import is `from pyfly.agentic.content import TextChunker`. The
deeper submodule paths (`content.chunking`, `content.compression`,
`content.markdown_chunker`) remain valid.

---

## Chunking (`content.chunking`)

### TextChunker

`TextChunker` splits a text string into overlapping chunks using one of three strategies:

- **token** (default) -- Splits by estimated token count.
- **sentence** -- Splits at sentence boundaries.
- **paragraph** -- Splits at paragraph boundaries (double newlines).

All `TextChunker` constructor arguments are keyword-only.

```python
from pyfly.agentic.content import TextChunker

chunker = TextChunker(chunk_size=4000, chunk_overlap=200, strategy="token")
chunks = chunker.chunk(long_text)
for c in chunks:
    print(f"Chunk {c.index}/{c.total_chunks}: {len(c.content)} chars")
```

Each chunk is a `Chunk` model with `content`, `index`, `total_chunks`, `source_start`,
`source_end`, `overlap_tokens`, and an open `metadata` dictionary. The `source_start` /
`source_end` fields record the chunk's offset in the original content (character offsets
for text, pixel-derived offsets for image tiles) so consumers can reconstruct provenance.

### DocumentSplitter

`DocumentSplitter` splits multi-document inputs at the boundaries matched by its
`separator` regex. The default separator is `r"\f|\n-{3,}\n|\n={3,}\n"`, which splits on
form-feed (`\f`) characters, a line of three or more dashes (`---`), and a line of three
or more equals signs (`===`). Segments shorter than `min_length` characters (default
`10`) are discarded. Both constructor arguments are keyword-only.

```python
from pyfly.agentic.content import DocumentSplitter

splitter = DocumentSplitter(min_length=50)
segments = splitter.split(raw_text)
```

Each segment is a `Chunk` whose `metadata` carries `{"type": "document_segment"}`.

### MarkdownChunker

`MarkdownChunker` is a structure-aware chunker that splits markdown at heading
boundaries and prepends the heading breadcrumb (e.g. `Guide > Setup > Install`) to every
chunk. It tokenises with `markdown-it-py` so that `#` characters inside fenced code
blocks and tables are never mistaken for headings. Sections whose body exceeds
`max_chunk_tokens` are split further by an internal `TextChunker`, and a final pass
hard-enforces the token budget against the real `tiktoken` BPE tokeniser (`cl100k_base`)
when it is installed, recursively splitting any body whose word-count estimate undercounts
the true token count (markdown tables in particular). All arguments are keyword-only.

```python
from pyfly.agentic.content import MarkdownChunker

chunker = MarkdownChunker(
    max_chunk_tokens=600,
    chunk_overlap=80,
    min_body_tokens=10,
    breadcrumb_separator=" > ",
)
chunks = chunker.chunk(markdown_text)
```

Each chunk's `metadata` carries the `breadcrumb` string for the section it came from.

### ImageTiler

`ImageTiler` computes tile coordinates for large images, enabling VLM processing
of high-resolution images that exceed the model's pixel budget.

All `ImageTiler` constructor arguments are keyword-only.

```python
from pyfly.agentic.content import ImageTiler

tiler = ImageTiler(tile_width=1024, tile_height=1024, overlap=128)
tiles = tiler.compute_tiles(image_width=4096, image_height=3072)
```

Each tile is a `Chunk` whose `metadata` contains `x`, `y`, `width`, `height`, `row`,
and `col` fields.

### BatchProcessor

`BatchProcessor` sends a list of chunks through an agent concurrently, with configurable
parallelism and an optional result aggregator.

`BatchProcessor` constructor arguments are keyword-only. `process()` accepts an optional
`prompt_template` format string (default `"{content}"`) where `{content}`, `{index}`,
`{total_chunks}`, and any chunk-metadata fields are interpolated, plus extra `**kwargs`
forwarded to `agent.run()`.

```python
from pyfly.agentic.content import BatchProcessor

processor = BatchProcessor(concurrency=4)
results = await processor.process(agent, chunks)
```

### Chunker protocol

`Chunker` is a runtime-checkable `Protocol` defining the pluggable chunking contract:
a single `chunk(content: str) -> list[Chunk]` method. `TextChunker`, `DocumentSplitter`,
and `MarkdownChunker` all satisfy it, so any of them can be passed where a `Chunker` is
expected.

---

## Compression (`content.compression`)

### TokenEstimator

`TokenEstimator` estimates the number of tokens in a string. By default it uses a
configurable words-to-tokens heuristic (`tokens_per_word`, default `1.33`). When
`tiktoken` is installed and an `encoding_name` is supplied (e.g. `"cl100k_base"`), it
uses the exact tokenizer instead, falling back to the heuristic if `tiktoken` is absent.
Constructor arguments are keyword-only. The `fits(text, max_tokens)` helper returns
`True` when the estimate is within budget.

```python
from pyfly.agentic.content import TokenEstimator

estimator = TokenEstimator(encoding_name="cl100k_base")  # exact, if tiktoken installed
tokens = estimator.estimate("Hello world, this is a test.")
within_budget = estimator.fits("...", max_tokens=4000)
```

### ContextCompressor

`ContextCompressor` reduces text to fit within a target token budget. If the text
already fits (per its `TokenEstimator`), `compress()` returns it unchanged; otherwise it
delegates to a pluggable strategy. The strategy is the first positional argument; an
optional `estimator` keyword overrides the default `TokenEstimator`. Note that
`compress()` is **async** and must be awaited.

- **TruncationStrategy** -- Hard truncation that keeps the beginning of the text.
  Keyword-only `tokens_per_word` and `suffix` (no `max_tokens` constructor argument;
  the limit is passed to `compress(text, max_tokens)`).
- **SummarizationStrategy** -- Uses an LLM agent to summarise. Takes the `agent` as its
  first positional argument plus a keyword-only `system_instruction`.
- **MapReduceStrategy** -- Chunks the text, summarises each chunk, then merges (with a
  final reduction pass if the merged summary is still over budget). Takes the `agent`
  positionally plus keyword-only `chunk_size` / `chunk_overlap`.

```python
from pyfly.agentic.content import ContextCompressor, TruncationStrategy

compressor = ContextCompressor(TruncationStrategy())
compressed = await compressor.compress(long_text, max_tokens=2000)
```

All three built-in strategies satisfy the runtime-checkable `CompressionStrategy`
protocol, whose single method is `async compress(text: str, max_tokens: int) -> str`.
Implement that contract to plug in a custom strategy.

### SlidingWindowManager

`SlidingWindowManager` maintains a sliding window over a stream of messages or chunks,
keeping total token usage within a budget by evicting the oldest items. Constructor
arguments are keyword-only and `max_tokens` defaults to `128_000`. It exposes the
`segment_count` and `estimated_tokens` properties for introspection and a `clear()`
method to reset the window.

```python
from pyfly.agentic.content import SlidingWindowManager

window = SlidingWindowManager(max_tokens=8000)
window.add("First message")
window.add("Second message")
current_context = window.get_context()
print(window.segment_count, window.estimated_tokens)
```

---

## Content sources (`content.sources`)

The sources layer abstracts *where* files come from before they are normalised and
chunked. `ContentSource` is a runtime-checkable `Protocol` describing a cursor-based
discovery contract: `list_changed(since)` yields `RawFile` descriptors, `fetch(file)`
downloads one to a local `Path`, and `current_cursor()` / `pending_cursor()` /
`commit_delta(cursor)` manage incremental delta tracking. Each `RawFile` carries
`source_id`, `name`, `mime_type`, `size_bytes`, `etag`, `fetched_at`, and a `metadata`
dict.

`LocalFolderSource` is the built-in filesystem implementation, configured by
`LocalFolderSourceConfig` (a `folder` path, an `include_hidden` flag, and an optional
`exclude_predicate` callable). It walks a directory tree and yields a `RawFile` per file.

```python
from pyfly.agentic.content.sources import (
    LocalFolderSource,
    LocalFolderSourceConfig,
)

source = LocalFolderSource(LocalFolderSourceConfig(folder="/data/corpus"))
async for raw in source.list_changed(since=None):
    path = await source.fetch(raw)
```


### Delegated cloud sources (`ContentSourceV2`)

The v2 adapter contract provides `SharePointSource`, `OneDriveSource` and
`GoogleDriveSource`. It complements the original `ContentSource`; existing local
sources and their cursor behavior are unchanged. Install `pyfly[agentic]` for the
HTTP dependencies. These adapters are the framework transport layer, not an
application's completed synchronization feature or evidence of live provider
qualification.

`Account` identifies a verified delegated provider subject, including the Microsoft
tenant where applicable. `SourceScope` binds that account to a stable drive ID.
`DelegatedTokenSource.access_token(account)` must retrieve a current token for
exactly that account. An application must resolve its current reader's connected
account; reusing an importer's token for other readers is not allowed. Display
names and email addresses do not establish that binding.

Each adapter supports the following operations:

| Operation | Result and responsibility |
| --- | --- |
| `start_cursor()` | Capture a future-change checkpoint; Google uses it before initial enumeration, while Graph initial import must use delta |
| `children(folder_id, cursor=None)` | One bounded page of direct children; caller owns bounded recursion and root membership |
| `changes(cursor)` | One ordered change page with a continuation or final checkpoint; do not checkpoint before observations and durable work are recorded |
| `get_item(item_id)` | Current metadata, including stable identity, provider version, parents and known size; unknown size remains `None` |
| `check_access(item_id)` | Current delegated content/download check; not a permission cache or application authorization decision |
| `download(item, sink, max_bytes=..., export_mime=None)` | Stream to unpublished staging and return measured bytes/hash, source scope, object/version and resulting MIME only after successful completion and version recheck |

Graph initial import must use `changes()` without a cursor for full drive delta
enumeration. A latest-token bookmark plus child paging is not a substitute for
Graph initial delta enumeration under concurrent changes. Preserve the order of repeated objects across pages and use the last
observation when reconciling a completed sequence. Google requires a captured
cursor; `drive_id="my-drive"` restricts the change feed to the My Drive hierarchy.
Use a separate explicit stable drive ID for each shared drive. Folder listing is
not recursive by itself, and shortcuts are never followed.

A Graph `deleted` observation is distinct from Google's `removed` observation:
Google removal can mean deletion **or loss of access**. HTTP access failures and
expired cursors raise `SourceError`; they never become invented deletion pages.
An incomplete listing cannot prove absence. Recovery must preserve prior origin
bindings and exclusions until a complete authorized enumeration is reconciled.
These rules follow [Graph delta](https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0)
and [Google change semantics](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes).

The following function receives application-owned credential and staging ports.
It performs no publication, local-file creation or remote-file mutation:

```python
from pyfly.agentic.content.sources import (
    AsyncByteSink,
    DelegatedTokenSource,
    DownloadReceipt,
    OneDriveSource,
    SourceScope,
)

async def stage_original(
    scope: SourceScope,
    tokens: DelegatedTokenSource,
    sink: AsyncByteSink,
    item_id: str,
    trusted_download_hosts: frozenset[str],
) -> DownloadReceipt:
    async with OneDriveSource(
        scope, tokens, download_hosts=trusted_download_hosts
    ) as source:
        item = await source.get_item(item_id)
        return await source.download(item, sink, max_bytes=25 * 1024 * 1024)
```

`AsyncByteSink.write(chunk)` receives at most 64 KiB per call. The caller owns
staging abort/cleanup after any exception or cancellation and must not publish
partial bytes. The adapter checks measured size, advertised size when present,
and source version again after transfer. A receipt is not an ingestion or index
commit. JSON pages are capped at 1 MiB and 200 items; each HTTP operation defaults
to a 30-second timeout. There are no automatic retries or whole-file buffers.
Trusted custom HTTP transports own any allocation they make before yielding a
chunk. Schedule throttled requests using the bounded `retry_after` value when
present; a missing value requires the application's bounded backoff policy.

Graph downloads can redirect to temporary signed content URLs. Configure exact
trusted HTTPS download hostnames; arbitrary hosts, IP literals, additional
redirects and cross-endpoint continuation links are rejected. Bearer headers are
never forwarded to a download host. The adapter suppresses HTTPX/HTTPCore request
logs only within the current provider task, preventing signed URLs or redirect
headers from entering those libraries' default logs. Custom transports and
external tracing must apply equivalent redaction. The default empty host set
intentionally cannot follow Graph download redirects. See the
[Graph download contract](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0).

Google checks the current account's `canDownload` capability. Ordinary files use
streamed original media. Google Docs, Sheets and Slides require an explicit
supported export MIME: DOCX, XLSX and PPTX respectively. Exports retain the original
object/version identity and record the measured exported bytes; no original size
is invented. The adapter enforces the smaller of the caller's limit and a conservative 10,000,000 bytes,
respecting the documented [Google export limit](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export).
Other native export types are unsupported by this first adapter contract.

### Source OAuth and application obligations

`SourceOAuthConfig` and `ReadOnlySourceOAuth` use the existing native `OAuth2Client`
for authorization-code/PKCE exchange and refresh. Source API transport exposes
GET only; token POST requests remain confined to the configured native OAuth
endpoint. No client-credentials fallback or remote mutation method is exposed.

Microsoft uses `openid`, `profile`, `offline_access` and delegated `Files.Read.All`
for accessible drive content, with an explicit tenant UUID or OneDrive `consumers`.
This contract does not implement tenant-wide SharePoint site discovery. Google
uses `openid`, `profile` and `drive.readonly`, with explicit offline consent.
`drive.file` is not used because it is a write-capable scope, and operators must
complete Google's requirements for restricted read scopes. See
[Google scope definitions](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).

Callbacks require HTTPS by default. For an explicitly configured local deployment,
`SourceOAuthConfig(..., allow_loopback_http=True)` also permits HTTP on exactly
`localhost`, `127.0.0.1` or `[::1]`; LAN addresses, other loopback spellings and
external HTTP hosts remain rejected. The URI must still match the provider
registration exactly. Source API and token endpoints remain HTTPS. This follows
the native OAuth endpoint policy and Google
[redirect validation rules](https://developers.google.com/identity/protocols/oauth2/web-server#uri-validation).

The authorization URL requires caller-owned state, nonce and an S256 PKCE
challenge. Before code exchange, the application must validate the exact callback,
state, expected issuer and replay protection. Returned ID tokens are opaque in
this wrapper; validate their signature, issuer, audience, expiry and nonce through
the identity layer before binding a source account.
Granted token scopes must include the requested read scope and may not exceed the
preset. Only the exact Google `https://www.googleapis.com/auth/userinfo.profile`
response alias maps to `profile`; unrelated email, write and unknown scopes are
not discarded. Google documents that
[response scope names can differ from request names](https://developers.google.com/identity/protocols/oauth2#3.-examine-scopes-of-access-granted-by-the-user),
and lists the canonical [profile scope](https://developers.google.com/identity/protocols/oauth2/scopes#oauth2).
Fresh code exchange still rejects missing scope evidence.

`refresh_token(refresh_token, granted_scopes=...)` requires the trusted scope tuple
from the previously validated, durably stored grant. It requests that exact
normalized subset. An omitted refresh-response scope retains only that requested
subset, as permitted by [RFC 6749 section 5.1](https://www.rfc-editor.org/rfc/rfc6749#section-5.1),
and returns it in `OAuth2Tokens.scope` for storage. An explicit response may narrow
the grant but may not expand it or lose the required read scope. The current
configuration is never treated as evidence of a prior grant. Encrypt refresh
credentials and serialize rotation in durable storage; preserve a rotated token
before another worker can reuse the old grant. If the response has no replacement
refresh token, retain the previously stored refresh token. Disconnect/revocation must pause
dependent admissions. Never substitute an application credential after failure.

Application synchronization still requires revisioned mapping review, durable
observations and origin/version bindings, leased jobs/outbox checkpointing,
monotonic publication fencing, exclusions and current source permission checks
for every reader. Compose these with the application's project/resource policy
before model input, search excerpts, previews, downloads, remembered context and
saved audit content, and recheck at output/commit fences. Do not hold a database
transaction during provider I/O. A local permission or an import receipt alone
does not authorize source content. Genuine delegated owned-folder journeys and
provider audit evidence remain required before claiming live connector support.

---

## Binary normalisation (`content.binary`, `[agentic-binary]` extra)

The binary subsystem turns a raw uploaded file (PDF, DOCX, XLSX, images, archives,
emails, ...) into one or more consumer-ready `BinaryArtifact` rows for a downstream
document loader or multimodal LLM. It is host-agnostic: configure it with a
`BinaryConfig` and inject plain handler classes -- no DI framework required. Heavy
third-party dependencies (`pypdf`, `Pillow`, `pillow-heif`, `cairosvg`, `py7zr`,
`extract-msg`) ship in the `pyfly[agentic-binary]` extra and are imported
lazily; a missing dependency surfaces as a typed error only when the relevant format is
actually encountered.

`BinaryNormalizer.normalise()` sniffs the real media type from magic bytes (never
trusting the declared Content-Type or extension), routes to the right handler, and fans
out into a list of `BinaryArtifact`, each carrying a `derived_from` ancestry tuple so a
member of an archive or email can be traced back to its parent bundle. Recursion depth
and total fan-out are bounded by `BinaryConfig`.

```python
from pyfly.agentic.content.binary import (
    BinaryConfig,
    BinaryNormalizer,
    build_office_converter,
)

config = BinaryConfig(office_converter="gotenberg", wrap_text_as_pdf=True)
normalizer = BinaryNormalizer(config=config, office=build_office_converter(config))
artifacts = await normalizer.normalise(raw_bytes, declared_media_type="application/pdf", filename="report.pdf")
for art in artifacts:
    print(art.kind, art.media_type, art.page_count, art.derived_from)
```

Key components:

- **`BinaryArtifact`** -- frozen output unit: `bytes`, `media_type`, `filename`, `kind`
  (canonical token such as `"pdf"`, `"image"`, `"docx"`, `"archive"`), `page_count`, and
  `derived_from`.
- **`BinaryConfig`** -- frozen tunables: `normalize_enabled`, `max_bytes`,
  `max_recursion_depth`, `max_expanded_files`, `max_uncompressed_bytes`,
  `wrap_text_as_pdf`, `email_render_header`, `office_converter`
  (`"none"`/`"gotenberg"`/`"libreoffice"`), plus Gotenberg/LibreOffice URL, path, and
  timeout fields.
- **`PdfGuard`** -- pre-flight integrity check; rejects encrypted PDFs
  (`EncryptedPdfError`) and corrupt/truncated PDFs (`CorruptPdfError`).
- **`ImageNormalizer`** / **`NormalisedImage`** -- converts HEIC/HEIF/AVIF/TIFF/BMP/SVG to
  PNG (or multi-page PDF for multi-frame TIFF); passes through PNG/JPEG/GIF/WebP.
- **`ArchiveUnpacker`** / **`EmailUnpacker`** -- fan out archives and emails into member
  files for recursive normalisation.
- **Office converters** -- `OfficeConverter` is the protocol;
  `build_office_converter(config)` returns `GotenbergConverter` (HTTP sidecar),
  `LibreOfficeConverter` (`soffice` subprocess), or `NoOpOfficeConverter` (the default
  pass-through) based on `BinaryConfig.office_converter`. `OFFICE_MEDIA_TYPES` is the
  frozenset of renderable MIME types.
- **`sniff_media_type(data, default=..., filename=...)`** -- magic-byte media-type
  detection used throughout the pipeline.
- **Error types** -- `BinaryNormalizationError` (base), `BinaryTooLargeError`,
  `UnsupportedBinaryError`, `CorruptPdfError`, `EncryptedPdfError`,
  `OfficeConversionError`, `ImageConversionError`, `ArchiveExtractionError`,
  `EmailParseError`, `BinaryFanoutError`.
