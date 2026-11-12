# What the index can't tell you

- Roles come from syntax. Inside macro bodies (`pass_baton! { ... }`, `matches!(...)`) tree-sitter
  doesn't parse, and the script falls back to reading the line. That fallback misreads a variant
  that opens a multi-line pattern.
- Prost enum fields appear as `i32`; the link to the enum is lost. Proto structs have fields but
  no `file:line`.
- Struct construction is detected for struct literals and tuple-struct calls (`Id(uuid)`), not
  for `new()`, `Default` or `.into()`. Those show up as uses of the function, not the type.
- The call graph is "function references function". Calls through trait objects resolve to the
  trait method, and functions registered by attribute macros (HTTP handlers under
  `#[conductor_macros::handler]`) have no caller in the index. Say so instead of inventing one.
- The index only knows Rust in one workspace at one commit.
