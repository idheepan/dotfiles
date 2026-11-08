-- Alt + h/a/o/e (left/down/up/right on Graphite) moves between nvim splits and
-- hands off to tmux panes at the edge. Matches the bindings in tmux.conf.
-- Also mapped in terminal mode so it works from nvim terminals like lazygit.
return {
  {
    "mrjones2014/smart-splits.nvim",
    lazy = false, -- must load at startup so tmux knows the pane is running nvim
    dependencies = {
      -- smart-splits v3 ships no tmux support; this backend sets @pane-is-vim
      { "smart-splits-nvim/backend-tmux", main = "smart-splits-backend-tmux" },
    },
    opts = {
      mux = { backend = "smart-splits-backend-tmux" },
      -- From a float (e.g. the lazygit terminal), go straight to tmux instead of
      -- focusing the previous window, which would close the float.
      move = { float = "mux" },
    },
    keys = {
      {
        "<M-h>",
        function()
          require("smart-splits").move_cursor_left()
        end,
        mode = { "n", "t" },
        desc = "Go to left split/pane",
      },
      {
        "<M-a>",
        function()
          require("smart-splits").move_cursor_down()
        end,
        mode = { "n", "t" },
        desc = "Go to lower split/pane",
      },
      {
        "<M-o>",
        function()
          require("smart-splits").move_cursor_up()
        end,
        mode = { "n", "t" },
        desc = "Go to upper split/pane",
      },
      {
        "<M-e>",
        function()
          require("smart-splits").move_cursor_right()
        end,
        mode = { "n", "t" },
        desc = "Go to right split/pane",
      },
    },
  },
}
