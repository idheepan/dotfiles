# Other commands

- `codemap crates [REGEX]`: Mermaid flowchart of workspace crate dependencies from
  `cargo metadata`, limited to crates whose name matches.
- `codemap verify DOC`: lists CamelCase type/variant names and `fn()` names in a markdown or HTML
  doc that don't exist in the index. A missing name is often from another branch, not invented:
  check `git log --all -S 'struct Name' --oneline` and index that rev before calling it wrong.
  Names from other repos (Atlantis Python, GUI TypeScript) will also show as missing.
- `codemap rebuild`: re-runs the ast-grep role scan and graph build on a cached index without
  rust-analyzer (seconds). Queries run it on their own when the graph is
  from an older `codemap.py`; call it yourself only to force a refresh after editing `roles.yml`.
- `codemap cache`: sizes and dates of cached indexes. Each commit costs about 100 MB; the shared
  clone and cargo target dir are reused across commits.

## Sharing as an Artifact

Run `codemap page SLUG --artifact`, which writes `artifact.html` without the document skeleton the
Artifact viewer adds, and publish that file. It needs nothing beyond Google Fonts.
