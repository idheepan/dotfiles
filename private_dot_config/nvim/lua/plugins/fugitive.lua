return {
  {
    "tpope/vim-fugitive",
    config = function()
      -- Optional configuration can go here
      vim.api.nvim_set_keymap("n", "<leader>gS", ":Git<CR>", { noremap = true, silent = true })
    end,
  },
}
