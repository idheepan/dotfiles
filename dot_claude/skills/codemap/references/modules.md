# Module dependencies and cycles

For "how are the modules connected" or "are there cyclic dependencies", start in the terminal:

```sh
codemap modules viking-barcode-sweep                  # edges, cycles, mutual pairs
codemap modules conductor --depth 1 --skip '!$'       # fold submodules, ignore macro references
```

An edge `a -> b` means code in module `a` names an item defined in `b` (a type, fn, variant, field,
const or macro), counted in distinct source lines with the names it reaches. The source module is
the innermost module around the reference, so `use` lines count; `mod b;` declarations and bare
module path segments don't. Re-exports resolve to where the item is defined. Cycles are the
strongly connected components (Tarjan), each listed with its edges fewest sites first, which are
the cheapest references to move. Mutual pairs (`a <-> b`) are listed separately because a large
component often hides them.

To render it, add `modules` to a filter (it can be the only key besides `crates`):

```json
{
  "title": "barcode-sweep module dependencies",
  "crates": ["viking-barcode-sweep"],
  "modules": {"depth": null, "skip": "!$", "externals": "^vital-host-shared"}
}
```

- `depth`: keep this many path segments (`1` folds `workflows::pack` into `workflows`). Use it when
  a crate has more modules than `max_nodes`.
- `skip`: regex over the referenced names as printed (`warn!`, `Pose::new`). `macro_rules!` macros
  live in the crate root, so every module that logs depends on `crate`, and that hides the design
  cycles. Render with and without `"skip": "!$"` and say which cycle comes from which.
- `externals`: regex over other crate names to draw as one node per crate. Without it, only
  modules of `crates` appear. `exclude` (default: test modules) applies to module paths.
- `"modules": true` takes the defaults.

This tab is laid out by Graphviz `dot` (`modules.dot` -> `modules.svg`), not mermaid: dot's layering
and spline routing stay readable at the edge density module graphs have. Modules and edges inside
a cycle are red, the page gets a Cycles section, and the inspector lists "Depends on" / "Depended
on by" with the names behind each edge and their sites.
