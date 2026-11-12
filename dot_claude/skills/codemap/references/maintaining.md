# Maintaining the skill

Read this before changing anything under `~/.claude/skills/codemap`.

- Edit with the Edit tool, never a Python `s.replace` helper. Edit reports a missing or ambiguous
  match and changes nothing; a helper that asserts `s.count(a) == 1` just dies when the text drifts.
  Re-read the lines you are changing right before the edit.
- Every file except `page.html` (UI glyphs) is ASCII-only (`->`, `<->`, `x`, plain hyphens). A literal em dash or arrow is what
  makes an Edit `old_string` fail to match when written as an escape. Run `codemap lint` after any
  change; it flags non-ASCII lines, checks that `page.html` holds each placeholder exactly once
  (`__TITLE__`, `/*__DATA__*/`, `<!--__SVGS__-->`, `<!--__NOTES__-->`, body markers), and
  syntax-checks the scripts.
- After changing `codemap.py` or `roles.yml`, bump GRAPH_VERSION if build() output changed (queries then rebuild stale graphs themselves), or run `codemap rebuild`; after changing `page.html`,
  `codemap page SLUG` is enough.

## graph.json schema

Use `codemap graph SLUG [summary|nodes REGEX|edges REGEX|cycles]` instead of loading it by hand.
Top-level keys: `meta`, `diagrams` (list of names), `tooBig` (null or counts), `nodes`, `edges`,
`cycles`, `mutual`.

- `nodes`: dict, node id -> `{label, path, crate, kind, def, sig, seed[, members, usedBy]}`.
  Iterating it yields id strings; use `.items()`.
- `edges`: list of `{d (calls|flow|types|modules), from, to, role, sites[], m[, via, names]}`;
  `from`/`to` are node ids.
- `cycles`: list of lists of node ids. `mutual`: list of `[id, id]` pairs.
