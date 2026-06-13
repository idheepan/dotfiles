return {
  "chomosuke/typst-preview.nvim",
  lazy = false, -- or ft = 'typst'
  version = "1.*",
  opts = {
    open_cmd = "open %s",
    dependencies_bin = {
      ["tinymist"] = vim.fn.exepath("tinymist"),
      ["websocat"] = vim.fn.exepath("websocat"),
    },
    extra_args = { "--verbose" },
  },
  keys = {
    {
      "<leader>cp",
      function()
        vim.cmd.TypstPreviewToggle()
      end,
      desc = "Typst Preview Toggle",
      ft = "typst", -- Only available in typst files
    },
  },
}
