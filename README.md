# Dotfiles

On Mac + orbstack, create a file `~/.config/chezmoi/chezmoi.toml` with this content:

```sh
[data.wezterm]
    [[data.wezterm.ssh_domains]]
        name = "orb"
        remote_address = "127.0.0.1:32222"
        username = "default"
        multiplexing = "WezTerm"
        [data.wezterm.ssh_domains.ssh_option]
            identityfile = "/Users/dhpn/.orbstack/ssh/id_ed25519"
            identitiesonly = "yes"
            userknownhostsfile = "/Users/dhpn/.orbstack/ssh/known_hosts" 
```
