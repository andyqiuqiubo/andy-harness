# JavaScript artifact reconstruction

REA can project an operator-supplied Electron/JavaScript application directory
or ASAR into [JavaScript Application Graph](javascript-application-graph.md)
without executing application code. The target-free
`analyze_javascript_application` MCP tool, dedicated
`rea analyze-javascript-application` CLI command, and generic
`rea analyze PATH` directory/ASAR route expose the same application
service and return an Evidence envelope containing the graph and an Electron
boundary summary.

The resulting Evidence can be combined with passive web or Electron capture
Evidence through
[`reconcile_javascript_runtime`](javascript-runtime-reconciliation.md). That
second operation does not rerun static analysis or contact CDP; it preserves
separate static, runtime, and cross-layer inference authorities.

## Public workflow

The tool reads the caller-selected local directory or ASAR path directly:

```bash
rea analyze /absolute/path/to/apps/app.asar --json
rea analyze-javascript-application /absolute/path/to/apps/app.asar \
  --integrity-policy record-and-continue --json
```

Generic `rea analyze` selects this static provider for directories and `.asar`
paths when neither `--provider` nor `--snapshot` is supplied. The dedicated
command and the generic route return their complete results inline. Native
targets, single JavaScript files, `.app` bundles, and explicit deep-provider or
snapshot requests retain the native deep-analysis route.

Both CLI routes emit `rea_progress` JSON lines on stderr as inventory, source
parsing, graph construction, validation, and Evidence hashing begin. Stdout
remains the selected result document. Completion is reported after Evidence
creation; a failed analysis retains its typed diagnostic result.

Configure an MCP client with the ordinary REA setup command:

```bash
rea setup
```

The equivalent MCP input is:

```json
{
  "input_path": "/absolute/path/to/apps/app.asar",
  "format": "auto",
  "integrity_policy": "fail"
}
```

`input_path` must be absolute. `format` accepts `auto`, `asar`, or `directory`.
`integrity_policy` accepts `fail` or `record-and-continue`; both CLI routes and
the MCP tool default to `fail`.
The result retains the canonical local path, root artifact digest, artifact
manifest and graph commitments, JavaScript Application Graph, static
Electron summary, reconstruction statistics, and explicit limitations. It does
not require a live Hopper, Ghidra, browser, or Electron process.

The application graph selects the shallowest `package.json` outside
`node_modules` as its package root, breaking equal-depth ties by Unicode
code-point order. Dependency manifests remain package observations but do not
declare Electron main or renderer entries. If only dependency manifests are
present, the inventory artifact remains the graph root.

## ASAR integrity

ASAR inventory checks Electron integrity metadata for embedded archive entries
and supplied `.asar.unpacked` companion files. An integrity failure identifies
the logical path, declared and calculated SHA-256 values, and whether the entry
was unpacked. By default, a mismatch is returned as a failure with its artifact
context. Application analysis can explicitly select `record-and-continue` to
analyze observed bytes while retaining each contradiction's declared and
observed hashes and `observed-untrusted` trust. Such results mark application
graph coverage partial and include every contradicted path in the limitations.
Mismatched nested ASARs stay opaque rather than being expanded. An unpacked
entry whose companion bytes were not supplied remains `unavailable`.

An unpacked entry whose companion bytes were not supplied remains
`unavailable`. REA continues analyzing embedded JavaScript and records the
missing native/resource bytes as unknown. See [what is reconstructed](#what-is-reconstructed)
for the inventory fields and [MCP integrity handling](mcp-contracts.md#integrity-record-and-continue)
for the tool-result contract.

## Large results

Application analysis projects semantic relationships one source file at a time.
After projection, it retains only the lexical module facts and exported return
shapes needed to build application relationships. Full source IR does not stay
resident for every file together. Callable ownership and contained-reference
queries use per-file range indexes, while call sites use exact-range lookups.
Existing node budgets and coverage reporting remain in effect.

Progress identifies the source file being parsed and projected. Cancellation is
checked between files and throughout result transfer. The owned analyzer can
be stopped while its synchronous parser or semantic extractor is active;
the CLI/MCP process remains available for control requests.

Graph and Evidence identifiers hash canonical JSON incrementally, without
assembling a single string for the whole graph. The canonical bytes and existing
identifiers remain unchanged. The opt-in regression check is
`npm run verify:javascript:digests`;
it hashes a value larger than the running Node engine's single-string limit.

Both application CLI routes stream complete JSON and JSONL output in bounded
chunks, including field filters and `--full-output`. They wait for each stdout
write instead of constructing or reparsing a document-sized string. The opt-in
`npm run verify:javascript:output` checks the actual CLI formatting boundary with
a temporary output file larger than the running engine's string limit, verifies
its bytes and digest, and removes it. Use `-- jsonl` for the compact JSONL check.
Each check needs space for one output file plus a 1 GiB free-space reserve.

Other CLI formats and `--token-count` still assemble whole strings.
Field selection remains useful when the caller needs a
smaller view, for example `--format json --filter-output
evidence_id,normalized_result.statistics`. Streaming output does not bound the
memory needed to construct the analysis graph itself.

JavaScript source parsing, static analysis and semantic projection run in one
owned analysis process, reused sequentially across files. Target code is never
executed. The analyzer defaults to 1024 MiB of V8 old-space and a 300000 ms
deadline per source, including startup and fact transfer. Select
`max_heap_mb` and `analysis_timeout_ms` through MCP, or `--max-heap-mb` and
`--analysis-timeout-ms` on the dedicated CLI command. These choices control the
analyzer; the caller's `NODE_OPTIONS` still controls the CLI/MCP process heap.
V8 old-space limits do not impose a process RSS limit.

A source that exhausts the analyzer heap does not terminate the CLI/MCP
process. Completed static facts are retained, other files can still be analyzed,
and unexamined semantic relationships remain explicit unknowns. The parent
also checks transfer expansion and accumulated results against its remaining
heap before accepting semantic facts. A resource-limited result has partial
coverage, the observed limits and an `analyze-javascript-source` unknown scope
with the specific failure, including captured diagnostics when available.
Resource failures do not increment syntax parse failures. There is no fixed
source-length cutoff.

Static observations also need memory when expanded into nodes, relationships
and Evidence. If that expansion does not fit, the file node retains
`static_analysis` and any admitted `semantic_module_analysis` observations,
including their exact values and source locations. Use the public modules item
view to read them. The graph marks those unexpanded relationships as unknown.
If cumulative result capacity is exhausted, analysis stops; inventoried but
unexamined files remain present with explicit unknown scopes.

A timeout stops the remaining analysis and returns typed failure details with
completed partial Evidence. MCP failure details return
`partial_observation: { kind: "retained-evidence", evidence_id: "..." }`
after recording succeeds, so `inspect_analysis_view` can open that analysis
directly without serializing the entire partial graph into the error response.
CLI failure output retains the complete partial Evidence.
Cancellation after source analysis begins likewise
retains completed facts; an SDK client may reject its cancelled request before
receiving that reply. On the same MCP connection, inspect the Evidence bundle
and use the retained analysis ID with `inspect_analysis_view`. Owned analyzer
cleanup is verified before its private files are released. Cleanup uncertainty
is reported separately and retains ownership for retry.

CLI workflows parse larger JSON input files incrementally from one verified
regular-file handle, without constructing a document-sized string. Smaller
files retain native JSON parsing. Both paths require strict JSON and valid
UTF-8, and return one complete value; the assembled object still needs memory.
An individual JSON string exceeding the native length limit or the available
heap headroom for assembly returns
`resource_constraint` with `input_reason: "too-large"`. Supply a smaller valid
value; for Evidence workflows, re-analyze a smaller selection of the original
target and use its complete Evidence. Splitting JSON text or trimming Evidence
fields does not produce a valid workflow input.

Evidence bundle and analysis snapshot file readers still decode whole
documents. Their runtime string-limit failures report a resource constraint
with the selected path, observed bytes and UTF-16 limit, rather than malformed
JSON.

`analyze_javascript_application` returns the complete analysis Evidence by
default. Select `"detail": "summary"` to retain that complete Evidence in the
current session and receive only its `inspect_analysis_view` summary: artifact
identity, statistics, Electron surface counts, application and semantic
coverage, limitations, and `normalized_result.parent_evidence_id`. Pass that ID
to `inspect_analysis_view` for module pages or items, or to the application
workflows as a retained reference. The summary does not repeat or truncate the
analysis; a server without session retention refuses summary detail with
`capability_unavailable`. CLI output is unchanged and always complete.

MCP prepares the complete repeated response incrementally against the pinned
SDK's 10 MiB stdio receive-buffer budget. Oversized results return an actionable
transport constraint and the exact same-session Evidence reference. Use
`inspect_analysis_view` with the retained Evidence ID for a summary, one
module, or a stable page of module identities. Use `trace_application_feature`
once a module, route, or string seed is known, or `export_evidence_bundle` to
write the complete canonical bundle without a document-sized allocation.
Same-session analysis reads reuse authenticated immutable snapshots; foreign
inline Evidence is still parsed and authenticated.
See [MCP tool results](mcp-contracts.md#tool-results) for larger client buffers
and `REA_MCP_MAX_RESPONSE_BYTES`. Follow-up results remain complete and can also
exceed the transport budget.

## What is reconstructed

The projector reuses the content-addressed artifact inventory and safe artifact
readers. It retains canonical artifact-relative paths, exact byte counts,
SHA-256 digests, inventory IDs, ASAR container identity, and `.asar.unpacked`
status. Direct ASAR inputs and filesystem-backed ASAR files nested beneath a
directory are supported.

JavaScript sources (`.js`, `.jsx`, `.mjs`, and `.cjs`) and TypeScript sources
(`.ts`, `.tsx`, `.mts`, and `.cts`) are parsed as inert text; analysis does not
execute them.

JavaScript and HTML source ranges retain an initial UTF-8 BOM as one UTF-16
code unit, matching the original bytes identified by the artifact digest.

If an ASAR declares an unpacked companion entry but the corresponding
`<archive>.unpacked` file is absent from the operator-supplied artifact set, REA
keeps the ASAR occurrence with `hash_status: unavailable`, records an explicit
limitation, and does not create a content-addressed child artifact for those
missing bytes. This allows static JavaScript/Electron reconstruction to proceed
for the embedded files while preserving the missing native/resource bytes as an
unknown instead of silently treating them as absent or verified.

Plain `.ts` artifact sources use TypeScript syntax without JSX, including
angle-bracket type assertions. `.tsx` and `.jsx` retain JSX parsing.

Selected bounded text is then parsed as inert data to recover:

- `package.json` metadata and declared main or renderer entrypoints;
- Electron preload paths and renderer files visible in static syntax;
- local HTML script entrypoints;
- Webpack and Rspack chunk registrations and module factories represented as
  AST literals;
- static imports, dynamic imports, CommonJS `require` calls, workers, and
  service workers;
- route, network endpoint, storage, and vendor-marker observations;
- local source-map declarations and original
  source names and content digests;
- explicit BrowserWindow options and `webPreferences`, including statically
  resolvable preload entrypoints;
- `contextBridge.exposeInMainWorld` and `exposeInIsolatedWorld` API keys and
  bounded literal member paths;
- renderer and main-process IPC operations, literal or dynamic channels, exact
  handler locations, and conservative pairing status;
- sender, frame, URL, and origin validation candidates without claiming that a
  visible check enforces a complete policy;
- utility-process entrypoints and native `.node` binding requests without
  parsing or executing the add-on.

Overloaded `.open` calls use lexical receiver facts: ambient browser window and
document receivers are treated as browsing-context or document operations,
while locally shadowed receivers can still contribute network endpoint
candidates. A template recovered with a parser error and no cooked value stays
dynamic; its raw spelling is not treated as a valid JavaScript string.

RPC client syntax also contributes network endpoint candidates, in both
`analyze_javascript_application` and `analyze_web_bundle`, distinguished by
`mechanism`:

- `trpc:procedure:<method>` records the static member path between the root
  expression and the method, such as `invoice.list` in
  `root.invoice.list.useQuery(...)`. tRPC proxies keep these property names
  through identifier minification. React hooks and TanStack option factories
  (`useQuery`, `useMutation`, `useInfiniteQuery`, `useSubscription`, suspense
  and prefetch variants, `queryOptions`, `infiniteQueryOptions`,
  `mutationOptions`, `subscriptionOptions`) are recognized directly. The
  vanilla `query` and `mutate` methods are recognized on a root bound to
  `createTRPCClient` or `createTRPCProxyClient` in the same lexical binding, or on a
  path of at least two segments in a source containing `TRPCClientError`,
  `trpc-accept`, or `/trpc`. Factory bindings must have one unconditional
  initializer and no binding assignments; shadowed locals do not inherit client
  identity. `subscribe` additionally requires a tRPC observer
  argument (`onData`, `onError`, `onStarted`, `onStopped`, `onComplete`, or
  `onConnectionStateChange`). `this` roots, browser globals, and any path
  containing an RTK Query `endpoints` segment are never procedures; cache-key
  helpers such as `queryKey` issue no request and are excluded.
- `trpc:link:<link>` records the literal `url` option of `httpLink`,
  `httpBatchLink`, `httpBatchStreamLink`, and `httpSubscriptionLink` when the
  link's callee name survives bundling, including `(0, module.httpBatchLink)`
  import-call wrappers.
- `graphql:query`, `graphql:mutation`, and `graphql:subscription` record a
  named operation found at the start of a string or cooked template literal —
  the name must be followed by variable definitions (`($`), a directive, or a
  selection set starting with a field name — or in a precompiled
  `OperationDefinition` document object. A graphql-tag `loc.source.body` copy is
  not reported twice. Anonymous operations are not reported.

A procedure path is the server-side procedure name, not a resolved URL; the
transport URL depends on the link configuration and batching. When the proxy is
reached through a member expression, such as a webpack module binding
(`l.S.invoice.list`) or a property holding the proxy (`o.api.post.byId`), the
recorded path keeps those leading carrier segments; the procedure path is its
suffix. Utility calls such as `useUtils().invoice.list.fetch()`, `useQueries`,
and links whose callee was renamed by a minifier are not recognized.

Each recovered bundle module retains the exact factory-source digest. A complete
bounded AST also receives a `babel-ast-v1` structural fingerprint that ignores
ordinary identifier names while retaining syntax, literals, operators, object
keys, and member properties. If fingerprint construction reaches its structural
bound, the fingerprint is unavailable and its status is `truncated`; a bounded
prefix is never presented as a complete stable fingerprint.

## Authority and unknowns

Artifact bytes and AST syntax are observations. Entrypoint resolution, imports,
loads, calls, and persistence relationships are static inferences and explicitly
say that syntax does not prove runtime execution. A malformed or unavailable
JavaScript file, package record, or source map produces an `unknown` graph scope
with `state: unavailable`; it is not treated as evidence that modules or source
are absent.

Source-map contents are parsed as local input. The graph stores original source names and optional content digests, not raw `sourcesContent` text.

Only explicitly present BrowserWindow values are observations. REA does not
substitute version-dependent Electron defaults for omitted `webPreferences`.
Dynamic option objects, bridge keys, API objects, and IPC channel expressions
remain unknown and make coverage partial.

Electron API identity follows proven lexical module aliases. Reassigned bindings,
dynamic selections, and visibly overwritten namespace members are unresolved.
Direct and destructuring assignment, loop, update, and delete targets are checked
through namespace aliases; unrelated member writes do not discard observed API
identity. This static check does
not evaluate arbitrary call side effects or runtime registration. Missing findings
do not establish that an application has no Electron boundary usage.

IPC pairing is an inference, not an observation. A renderer `invoke` pairs only
with one unique `ipcMain.handle`/`handleOnce` candidate on the same exact literal
channel; a renderer send pairs only with one unique `ipcMain.on`/`once`
candidate. Dynamic channels are never paired, and multiple compatible handlers
are reported as ambiguous without adding a caller-to-handler edge. A matching
channel still does not prove registration order, reachability, or runtime use.

For `.node` bindings, member names mean “requested by JavaScript syntax.” A
resolved add-on path does not convert them into verified native exports. Native
symbol verification remains a separate deep-analysis claim.

Endpoint observations preserve useful local diagnostics, including query values
and fragments, while removing only URL username/password credentials. Artifact
paths, digests, parse locations, and analysis metadata remain actionable
because REA is local-only.

## Safety boundary

The reconstruction path never uses `eval`, `Function`, `vm.runInContext`, a DOM,
bundle `push` handlers, or application bootstrap code. The Webpack/Rspack fixture
used by the test suite mutates a global and throws if executed; reconstruction
recovers its four module factories without triggering either side effect.

Directory readers do not follow symlinks. Artifact paths pass through the shared
normalizer and collision registry, ASAR entry bytes are rechecked against the
inventory digest before parsing, and malformed ASAR operations return typed
format diagnostics that retain the local container path. Native add-ons are
represented by metadata only.

## Bounds and coverage

The reconstruction keeps internal resource safeguards for artifact entries,
cumulative artifact bytes, bytes per entry, compression ratio, path depth and
length, selected text files, text bytes, AST nodes, and cooperative parse time.
These safeguards are not caller-selectable output budgets. Source-map parsing
reports truncation when its format or parser safety boundary is reached. The
application graph has no aggregate node, edge, root, or observation prefix cap.

Semantic evaluation retains at most 256 distinct primitive candidates per
expression and at most 1 MiB of worst-case JSON string bytes across a normalized
primitive value's string candidates. The byte budget estimates six JSON bytes
per UTF-16 code unit, the expansion bound for escaped strings, and bounds both
retained candidate strings and temporary canonical-key serialization. This is
an in-memory semantic allocation bound independent of CLI or MCP transport
budgets. Source literal bytes remain in the parsed source and artifact evidence;
when a normalized semantic value exceeds the bound, its value is unknown and
coverage is partial. Expression
evaluation and provenance walks stop after 256 nested levels. Union products
and string lengths are checked before allocating combinations or concatenated
strings. When a boundary is reached, the value stays unknown, semantic coverage
becomes partial, and graph evidence retains the expression location and typed
limiting reason. The number of unresolved alternatives is unknown; it is not
reported as an exact omission count.

Byte, entry, path, and graph-shape bounds are hard limits. The parse deadline is
checked before and between bounded parsing and traversal phases; the synchronous
Babel and JSON parser calls cannot be preempted mid-call, so their input byte
bounds remain the hard protection for an individual call.

Byte-identical assets share a content-digest node. Every distinct observation
and inventoried containment edge is retained. If parsing or artifact safety
boundaries omit input, coverage reports that omission; when an exact omission
count is not knowable, `omitted_count` is `null` rather than a guessed value.

## Verification boundary

The source-owned fixture covers an extracted directory, a direct ASAR with an
unpacked native add-on, a direct ASAR whose unpacked native companion is
missing, and an ASAR nested beneath a directory. Tests also cover deterministic
reruns, parse-not-execute behavior, source-map parsing, global source-map
limits, malformed structured data, invalid containers, traversal, symlink
escape, oversized text, cancellation, AST truncation, and repeated content
identities. A separate synthetic Electron fixture covers explicit safe and
unsafe preference values, preload and contextBridge surfaces, literal, dynamic,
paired, ambiguous, and unpaired IPC, validation candidates, utility processes,
and native binding requests. These fixtures establish parser and artifact-reader
claims; they do not replace the later operator-supplied real-application
benchmark.

URI schemes are classified independently of local file names: a reference such
as `web3:app.js` remains external even when an artifact has that literal name.
Scheme characters may include digits after the initial letter; a relative path
such as `./web3:app.js` still names a local artifact.

Package `exports` fallback arrays are supported both at the top level and under
the root `"."` entry. The resolver selects targets in declared order using the
same conditional and invalid-entry handling as nested exports arrays.
For imports and requires, the active Node conditions include `module-sync`;
earlier active conditions retain precedence over later ones.

ESM relative module paths and selected package exports targets use URL suffix handling
and one percent-decoding pass. CommonJS relative paths and legacy package main
fields retain literal filename punctuation. Exports targets must start with
`./` and exclude `.`, `..`, and `node_modules` path segments, including their
encoded forms, before URL normalization. Arrays skip invalid exports targets
in declared order; URL decoding and file lookup failures for a selected target
do not select a later entry. Rejected references retain the package metadata
path, original target, failed constraint, and importing source location.

HTML script references resolve to exact inventoried files after applying the
document base and query/fragment rules. As in a browser, the script URL and base
href are read without surrounding whitespace or embedded tabs and newlines, and
their percent-encoded path bytes are decoded; encoded dot and separator bytes
are still rejected. CommonJS module lookups retain extension and directory
resolution.
Unresolved HTML references retain their declaration, source range, and resolution
reason in the renderer observations.
HTML script source ranges follow the HTML parser across LF, CRLF, and bare CR
line endings, preserving UTF-16 columns.
