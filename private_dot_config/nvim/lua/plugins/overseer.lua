return {
  {
    "stevearc/overseer.nvim",
    dependencies = { "folke/trouble.nvim" },
    config = function(_, opts)
      local overseer = require("overseer")
      overseer.setup(opts)
    end,
    opts = {
      output = {
        use_terminal = true,
        preserve_output = false,
      },
      component_aliases = {
        default = {
          "on_exit_set_status",
          "on_complete_notify",
          { "on_complete_dispose" },
          -- Parse compiler output (gcc/clang-like)
          { "on_output_parse", problem_matcher = "gcc" },
          -- Show output in a floating window
          { "open_output", direction = "dock", focus = true, on_start = "always" },
        },
        -- Ensure VSCode tasks get the same defaults
        default_vscode = {
          "default",
        },
      },
    },
  },
}
