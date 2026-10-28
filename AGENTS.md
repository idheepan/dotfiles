# AGENTS.md

Personal dotfiles managed by [chezmoi][chezmoi]. This directory
(`~/.local/share/chezmoi`) is the *source state*; `chezmoi apply` renders it
into `$HOME`.

## Target platforms

Every change must keep working on all four. Windows and other OSes are out of
scope, so don't add branches or files for them.

- **Standalone NixOS**: `.chezmoi.osRelease.id` is `"nixos"`. System packages,
  LSPs and formatters come from the flake in `~/.nixos`, which is *not* in this
  repo. Mason is disabled and tools are expected on `PATH`. Prebuilt binaries
  that a plugin downloads often fail to run because they aren't patched for the
  Nix store, so build them from source instead.
- **NixOS VM on macOS (OrbStack)**: NixOS plus `headless = true` in
  `chezmoi.toml`. There's no GUI here. Host services are reachable at
  `host.orb.internal`, and the Mac's Ghostty usually connects in over SSH.
- **macOS**: `.chezmoi.os` is `"darwin"`. It uses zsh, LaunchAgents under
  `private_Library/`, and a GUI terminal.
- **Arch Linux**: `.chezmoi.osRelease.id` is `"arch"`. The desktop is a niri
  Wayland session (waybar, fuzzel, mako, hypridle/hyprlock, kanshi), and Mason
  manages the editor tooling.

## Layout

- Paths follow chezmoi source-state naming: `dot_`, `private_`, `executable_`,
  `symlink_`, and a `.tmpl` suffix for templates. Put a new app config at
  `private_dot_config/<app>/`, matching what's already there.
- `private_dot_config/nvim/` is LazyVim, with one file per plugin spec in
  `lua/plugins/`.
- `private_dot_config/tmux/`: `tmux.conf` sources files from `modules/`, and
  third-party plugins are pulled in by the `.chezmoiexternal.toml` in that
  directory. Don't vendor plugins.
- `dot_claude/` holds Claude Code settings and hooks. `dot_local/bin/` holds
  user scripts. `claude-tmux` connects Claude hooks to the tmux status bar.
- Shells: on Linux it's bash (`dot_bashrc`, `dot_bash_profile`), and on macOS
  it's zsh (`dot_zshrc`). An alias or env change usually belongs in both.

Repo-only files (this file, `chezmoi.sample.toml`) get copied into
`$HOME` unless their name starts with `.` or they're listed in
`.chezmoiignore`. After adding one, check `chezmoi managed`.

## Making a config portable

Use the first option that works:

1. **Portable at runtime**: let the config check for itself, as in
   `[ -d /opt/android-studio ]` in `dot_bashrc`. No template needed.
2. **Ignore per platform**: skip whole files in `.chezmoiignore` or
   `.chezmoiignore.tmpl`. chezmoi reads both, and both are templates.
3. **Template**: add the `.tmpl` suffix only when the content itself has to
   differ.

To detect NixOS, use the existing idiom. `osRelease` doesn't exist on macOS,
so always guard it with `hasKey`:

```text
{{- $is_nixos := false -}}
{{- if hasKey .chezmoi.osRelease "id" -}}
  {{- if eq .chezmoi.osRelease.id "nixos" -}}
    {{- $is_nixos = true -}}
  {{- end -}}
{{- end -}}
```

### Machine data

Machine-specific values go under `[data]` in
`~/.config/chezmoi/chezmoi.toml`, never hardcoded. Current keys:

- `headless` (top-level): read by `.chezmoiignore` and
  `lazygit/config.yml.tmpl`.
- `llama.endpoint_fim`: optional, falls back to localhost.

chezmoi errors on a missing key. When you add a key, read it defensively with
`index` and a fallback (see `completion.lua.tmpl`),
and document it in `chezmoi.sample.toml`.

## Workflow

- Edit files here, never the rendered copies in `$HOME`, because
  `chezmoi apply` overwrites those.
- Preview with `chezmoi diff`. Ask before running `chezmoi apply`, since it
  changes the live home directory.
- To render a template for another platform, override the data:

  ```bash
  chezmoi execute-template --override-data \
    '{"chezmoi":{"os":"darwin","osRelease":{"id":"darwin"}},"headless":false}' \
    < path/to/file.tmpl
  ```

  Always override `osRelease.id` as well. Otherwise it keeps this machine's
  value, so on NixOS a "darwin" render still takes the NixOS branch. For Arch,
  use `{"chezmoi":{"osRelease":{"id":"arch"}}}`. Check every platform a
  template branches on, and check `chezmoi ignored` when you touch the ignore
  files.
- Linters: `markdownlint-cli2` (config in repo), `stylua` for Neovim Lua,
  `shellcheck` for shell scripts.
- Commit messages are a short imperative sentence with no type prefix, for
  example `Show 7 day reset time on tmux claude panel`.

[chezmoi]: https://www.chezmoi.io/
