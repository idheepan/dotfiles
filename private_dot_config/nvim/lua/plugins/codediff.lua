return {
  {
    "esmuellert/codediff.nvim",
    dependencies = { "MunifTanjim/nui.nvim" },
    cmd = "CodeDiff",
    keys = {
      { "gd", false },
      {
        "<leader>gd",
        "<cmd>CodeDiff<cr>",
        desc = "CodeDiff",
      },
      {
        "<leader>gC",
        ":CodeDiff ",
        desc = "CodeDiff (with args)",
      },
    },
    opts = {
      explorer = {
        view_mode = "tree",
      },
    },
  },
}
