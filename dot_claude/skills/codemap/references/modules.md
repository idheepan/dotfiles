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

- `depth`: keep this many path segments (`1` folds `workflows::pack` into `workflows`). For a
  "module diagram of crate X" request, render two slugs from the start: `<crate>-modules-overview`
  with `"depth": 1` and `<crate>-modules` unfolded. Past about 20 modules the unfolded graph can't
  be read at fit-to-width (`render` prints `dense`); the overview is the one to lead with.
- `skip`: regex over the referenced names as printed (`warn!`, `Pose::new`). `macro_rules!` macros
  live in the crate root, so every module that logs depends on `crate`, and that hides the design
  cycles. Render with and without `"skip": "!$"` and say which cycle comes from which.
- `externals`: regex over other crate names to draw as one node per crate. Without it, only
  modules of `crates` appear. `exclude` (default: test modules, `*_tests`, `test_support`)
  applies to module paths.
- `"modules": true` takes the defaults.

## Reading the edges correctly

- An edge points at the module that defines the item, not the path the code wrote. A
  `use crate::domain::qc::{Started, TubeScan}` that goes through re-exports in `domain/qc/mod.rs`
  still counts as edges to `domain::qc::start` and `domain::qc::tubes`. So the graph can't tell
  "imports the child directly" from "imports through the parent's re-export", or show whether a
  re-export is used. Read the `use` lines (the edge's sites) before saying either. A claim of
  "the re-exports are only partly used" from module edges was wrong for this reason.
- A cycle between a parent and its children (`a <-> a::b` for each child) is usually the
  parent-as-prelude shape: `mod.rs` re-exports the children, and the children use shared errors
  and helpers from `mod.rs`. Name it as such before treating it as a design problem.

## Proposing a fix for a cycle

"Fewest sites first" ranks the edges by how cheap they are to cut. It doesn't say which cut is
good design. Moving a type to wherever breaks the cycle can put it in a module whose purpose it
doesn't share, and a user rejected exactly that suggestion (moving a run-lifecycle enum into the
fixture schema). For each candidate move, before proposing it:

- Read the destination's `//!` doc and list what already lives there (`codemap graph SLUG
  by-module` on a classes map, or `codemap find '^dest::' --kinds Struct,Enum`). Say why the item
  belongs with those, not only that the cycle goes away.
- Split an item if only part of it belongs there: an enum can move while its `impl` block stays
  in the old module, since Rust allows impls anywhere in the crate.
- Give more than one option (move, split out a new leaf module, invert the dependency through a
  parameter or trait, or accept the cycle) and the trade-off of each.
- Offer the `classes` map (SKILL.md) so the user can judge where the types live.

## Layout

This tab is laid out by Graphviz `dot` (`modules.dot` -> `modules.svg`), not mermaid: dot's layering
and spline routing stay readable at the edge density module graphs have. Modules and edges inside
a cycle are red, the page gets a Cycles section, and the inspector lists "Depends on" / "Depended
on by" with the names behind each edge and their sites.
