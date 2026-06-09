return {
  -- 1. Install Catppuccin
  {
    "catppuccin/nvim",
    name = "catppuccin",
    priority = 1000, -- Load this first
    opts = {
      flavour = "mocha", -- latte, frappe, macchiato, mocha
    },
  },

  -- 2. Tell LazyVim to use it as the active theme
  {
    "LazyVim/LazyVim",
    opts = {
      colorscheme = "catppuccin",
    },
  },
}
