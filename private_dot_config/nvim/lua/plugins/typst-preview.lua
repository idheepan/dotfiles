return {
  "chomosuke/typst-preview.nvim",
  lazy = false, -- or ft = 'typst'
  version = "1.*",
  opts = {}, -- lazy.nvim will implicitly calls `setup {}`
  keys = {
    {
      "<leader>cp",
      function()
        -- The command is usually named after the filetype/plugin.
        -- You may need to confirm the exact command, but :TypstPreview is a common convention.
        vim.cmd.TypstPreview()
      end,
      desc = "Typst Preview Toggle",
      ft = "typst", -- Only available in typst files
    },
  },
}
