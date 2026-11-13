# Maintaining the skill

Read this before changing anything under `~/.claude/skills/codemap`.

- Edit with the Edit tool, never a Python `s.replace` helper. Edit reports a missing or ambiguous
  match and changes nothing; a helper that asserts `s.count(a) == 1` just dies when the text drifts.
  Re-read the lines you are changing right before the edit.
- Every file except `page.html` (UI glyphs) is ASCII-only (`->`, `<->`, `x`, plain hyphens). A literal em dash or arrow is what
  makes an Edit `old_string` fail to match when written as an escape. Run `codemap lint` after any
  change; it flags non-ASCII lines, checks that `page.html` holds each placeholder exactly once
  (`__TITLE__`, `/*__PALETTE__*/`, `/*__DATA__*/`, `<!--__SVGS__-->`, body markers), and
  syntax-checks the scripts.
- Colors live only in `scripts/palette.json` (`[light, dark]` per token). `codemap lint` rejects a
  hex color in `codemap.py`, `page.html`, `mermaid.json` or `codemap`, and checks every pair in the
  palette's `contrast` list in both themes, so raise a value there rather than loosening a minimum.
  `palette_css()` turns the tokens into one stylesheet used by the page and embedded in every
  finished `.svg`; emitters write classes (`depth-N` on clusters, `cycle`), never colors.
- `page()` runs `finish_svg()` on each diagram and rewrites the `.svg` in place: node classes from
  graph.json (`kind-*`, `seed`, `gen`), kind badges on flowchart boxes (the hidden `BADGE_PAD`
  first word reserves their room), `gen`/`ext` tags, edge `data-role`, the palette stylesheet.
  A finished SVG only gets its stylesheet replaced, so after a palette edit `codemap page SLUG`
  re-themes both the page and its `.svg` files; badges on maps rendered before the badge change
  need `codemap render SLUG`.
- After changing `codemap.py` or `roles.yml`, bump GRAPH_VERSION if build() output changed, then run `codemap rebuild`; after changing `page.html`,
  `codemap page SLUG` is enough.

## graph.json schema

Use `codemap graph SLUG [summary|nodes REGEX|edges REGEX|cycles]` instead of loading it by hand.
Top-level keys: `meta`, `diagrams` (list of names), `nodes`, `edges`,
`cycles`, `mutual`.

- `nodes`: dict, node id -> `{label, path, crate, kind, def, sig, seed[, members, usedBy]}`.
  Iterating it yields id strings; use `.items()`.
- `edges`: list of `{d (calls|flow|types|classes|modules), from, to, role, sites[], m[, via, names]}`;
  `from`/`to` are node ids. Field edges (`types`, `classes`) carry the member lines that name the
  target; only fallback edges from generated types without a source span have empty `sites`.
- `cycles`: list of lists of node ids. `mutual`: list of `[id, id]` pairs.
