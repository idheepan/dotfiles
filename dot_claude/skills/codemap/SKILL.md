---
name: codemap
description: Deterministic maps of Rust code from a rust-analyzer SCIP index (cached per commit): types behind a feature, who constructs or matches each type/variant, call chains, type tables, module dependency cycles, crate graphs, and checking a doc or diagram against the code. Use for any request to visualize, diagram, map or explain Rust types, data flow or module/crate dependencies in a cargo workspace such as device-sw, even if "codemap" is never said.
---

# codemap

Draw Rust structure from an index instead of from reading. Every node and edge comes from
rust-analyzer's SCIP index plus an ast-grep syntax-role scan, so diagrams are reproducible and
each arrow has a `file:line`. Your job is the part that needs judgment: choosing which symbols
answer the user's question, keeping the picture small enough to read, and explaining it. Never
add an edge the index doesn't have; if the explanation needs one (runtime dispatch, channels,
HTTP), state it as an inference in prose.

Everything runs through `~/.claude/skills/codemap/scripts/codemap` (written `codemap` below);
`codemap help` lists every command. Common options: `--repo DIR` (default: the git repo containing
the current directory), `--rev REV` (default `HEAD`), `--ws DIR` (cargo workspace, default `host`;
`firmware` also exists in device-sw), `--shell NAME` (devshell, default = `--ws`).

A SLUG names an output folder, `tmp-docs/07-generated/codemap/<slug>/` relative to the current
directory (override with `$CODEMAP_OUT`). Commands taking SLUG also take a plain path.

## 1. Indexing happens by itself

Run any query (`find`, `refs`, `render`, ...). If the tree is not indexed the command starts the
index in the background, waits up to 100 s, and exits 4 with "still indexing": run the same
command again, or `codemap wait`. A cold index takes 2-3 min, so expect one repeat.
Exit codes: 0 done, 3 not indexed (`status` only), 4 still running, 1 failed (log tail printed).

- Uncommitted work is included. With the default `--rev HEAD` and a dirty repo, the index is of the
  working tree: tracked edits plus untracked files up to 2 MB each (docs `*.md` are ignored). It is
  keyed `commit+hash`, so each new edit state indexes once and identical states reuse the cache.
  `codemap status` shows `tree: <commit> + working tree <hash>`. `--committed` indexes HEAD alone.
- `--rev REV` indexes another commit (committed content only), also for other worktrees.
- Untracked crates are fine: the working-tree snapshot is committed inside the cache's private
  clone before the devshell is entered, so flake evaluation sees them. Your repo is never touched.
- A graph from an older `codemap.py` is rebuilt automatically (about 5 s); `codemap rebuild` is
  only needed to force it.
- `codemap index --fg` runs in the foreground; the worker log is under `~/.cache/codemap/run/`.

## 2. Find the seed types

Turn the user's question into a few concrete types before rendering anything.

```sh
codemap find '::(Barcode|PackValidation)$' --kinds Struct,Enum,Trait,TypeAlias   # --limit N (default 60)
codemap refs 'pre_analytical::PackBarcodeSource$' --by-fn                   # one line per enclosing fn
```

`find` matches a regex against `module::path::Name` (no crate name, see "How seeds are selected") and prints kind, crate, location and
signature (proto types show `external`: generated source). `refs` lists each reference as
`constructs`, `matches` or `uses` with its enclosing function. Start with `--by-fn` or the default
cap of 25 per type, and ask for more only for a type you care about. Grep and reading the code
are fine here too.

## 3. Write a filter and render

Save `filter.json` in the slug folder so the user can tweak and re-run it.

```json
{
  "title": "Pack barcode source to validation state",
  "question": "Which conductor functions decide a pack's validation state, and from which barcode source?",
  "seed_types": "::(PackBarcodeSource|ConsumableBarcodeSource|PackValidation|Gs1Missing)$",
  "crates": ["conductor"],
  "exclude": "(^|::)tests?(::|$)",
  "roles": ["constructs", "matches"],
  "max_nodes": 30,
  "max_members": 12
}
```

- `seed_types`: types (Struct, Enum, Trait, TypeAlias, Union) selected by regex; see "How seeds
  are selected" below. Give a list of anchored regexes, e.g. `["::PackValidation$", "::PackKind$"]`.
- `seed_fns`, `callers` (default 3), `callees` (default 1): draw a call graph walked up and down
  from the matching functions (kinds Function, Method, StaticMethod, TraitMethod). Use it whenever the question is about a flow ("how does X get to
  the PATCH"): the type view alone only connects functions through shared types, and hub types
  such as error enums swamp it. Seed the function where the flow ends (the sender) and raise
  `callers` until the entry points (`subscribe`, HTTP handlers) appear. A second seed upstream
  (the validator, the parser) shows where the data comes from.
- `crates`: limits both seed types and the functions shown. Crate names are package names
  (`conductor`, `vital-host-shared-proto`, `ch-driver`), not directory names.
- `roles`: drop `matches` to see only producers, or the reverse.
- `max_nodes`: above this the page leaves the diagrams out and says why (the tables stay).
  Narrow the filter rather than raising the limit: around 30 nodes is where a diagram stops
  being readable. Drop types like `BackendError` that every caller matches; they turn into hubs.
- `modules`: module dependency graph and cycles, see `references/modules.md`.

### How seeds are selected

`seed_types` and `seed_fns` (and `codemap find` / `refs`) all use the same rules:

1. A regex is tested with `re.search` against the symbol path `module::path::Name`. The path
   has NO crate name and no leading `::`: `pack_barcode::PackKind`, `Pack` (crate root), or for a
   method `PackValidator::validate` (`SelfType::method`, also for trait impls). `conductor::Foo`
   never matches; scope by crate with `crates`. A leading `::` is accepted for top-level items,
   so `::Name$` finds both `Name` and `a::Name`.
2. Only the right kinds are considered: types for `seed_types`, fns/methods for `seed_fns`. An
   enum variant is not a seed; pick its enum and the variants come with it. A fn pattern that
   names a type (or the reverse) selects nothing.
3. Then `crates` drops other packages (package names, `-` or `_`: `ch-driver`, `conductor`) and
   `exclude` drops paths (default: test modules).
4. A string with top-level alternation, `(^a::A|^b::B)$`, is split and reported per alternative.
   Prefer a list of anchored regexes: no precedence mistakes, one report line each.
5. Unanchored regexes match substrings: `::Pack` selects `PackKind`, `PackValidation` and
   `DiscPackInfo`. Anchor both ends (`^pack_barcode::Pack$`, or `::Pack$`).

Check before rendering, since seeds that don't match are the usual failure:

```sh
codemap seeds <slug>     # lists what each pattern selected; for a miss says why (other kind,
                         # other crate, excluded, nearest names). Exit 2 if a key selected nothing.
```

`render` prints the same report and refuses to render (exit 2, output folder untouched) if
`seed_types` or `seed_fns` selected nothing. A key that is misspelled (`seeds_fns`), a crate that
doesn't exist, or a bad regex/role/kind is an error too, never silently ignored.

```sh
codemap render <slug>
```

Writes `index.html` (self-contained page with diagram tabs, inspector, find, edge and type
tables, and your notes panel) plus `graph.json`, the `.mmd`/`.svg` diagrams and a filter copy.
Don't open the SVGs; the summary below and the page are the interface. To inspect `graph.json`,
use `codemap graph <slug> [summary|nodes REGEX|edges REGEX|cycles]` rather than writing Python.

## 4. Judge the layout from the summary

`render` ends with the diagrams it drew, node/edge counts, and the busiest types, flagged when one
dominates, or `TOO BIG` with the hubs. That is usually enough: if a type dominates, exclude it or
split the question into two filters. Take a screenshot only when the summary looks fine but the
result is suspect, since reading an image is expensive:

```sh
codemap shot <slug>       # writes <slug>/screenshot.png and prints its path; read it
```

## 5. Explain it

Write your interpretation to `<slug>/notes.html` as an HTML fragment (`<p>`, `<h3>`, `<ul>`,
`<code>`, and `<em>Inference:</em>` to mark claims the index can't show), then run
`codemap page <slug>` to rebuild `index.html` without touching the diagrams. Re-rendering keeps
`notes.html`. Tie each claim to an edge or a `file:line` from the page. Mark anything the index
can't show (trait-object dispatch, channel sends, HTTP calls, GUI round trips) as inference and
say how you know.

Give the user the path to `index.html`.

## References (read only when needed)

- `references/modules.md`: `codemap modules`, cycles, mutual pairs, the `modules` filter key.
- `references/other-commands.md`: `crates`, `verify`, `rebuild`, `cache`, sharing as an Artifact.
- `references/maintaining.md`: editing this skill (ASCII-only files, `codemap lint`, graph.json schema).
- `references/limits.md`: what the index can't show (macros, prost enums, `new()`, trait objects,
  attribute-registered handlers). Read it before explaining a flow that crosses any of these.
