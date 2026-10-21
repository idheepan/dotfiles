-- Alt + h/a/o/e (left/down/up/right on Graphite) moves between nvim splits and
-- hands off to tmux panes at the edge. Matches the bindings in tmux.conf.
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
    },
    keys = {
      {
        "<M-h>",
        function()
          require("smart-splits").move_cursor_left()
        end,
        desc = "Go to left split/pane",
      },
      {
        "<M-a>",
        function()
          require("smart-splits").move_cursor_down()
        end,
        desc = "Go to lower split/pane",
      },
      {
        "<M-o>",
        function()
          require("smart-splits").move_cursor_up()
        end,
        desc = "Go to upper split/pane",
      },
      {
        "<M-e>",
        function()
          require("smart-splits").move_cursor_right()
        end,
        desc = "Go to right split/pane",
      },
    },
  },
}
