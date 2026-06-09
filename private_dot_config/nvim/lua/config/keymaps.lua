-- Keymaps are automatically loaded on the VeryLazy event
-- Default keymaps that are always set: https://github.com/LazyVim/LazyVim/blob/main/lua/lazyvim/config/keymaps.lua
-- Add any additional keymaps here

-- Save file easily
vim.keymap.set({ "n", "i" }, "<C-s>", "<ESC><cmd>update<CR>", { desc = "[S]ave changes to buffer" })

-- Add newline below without moving cursor or entering insert mode
vim.keymap.set("n", "<cr>", "o<esc>", { desc = "add blank line below" })

-- Scroll Down (Ctrl-d) and center (zz)
vim.keymap.set("n", "<C-d>", "<C-d>zz", { desc = "Scroll Down and Center View" })

-- Scroll Up (Ctrl-u) and center (zz)
vim.keymap.set("n", "<C-u>", "<C-u>zz", { desc = "Scroll Up and Center View" })

-- Smart Splits integration
vim.g.smart_splits_multiplexer_integration = true

vim.keymap.set("n", "<C-h>", require("smart-splits").move_cursor_left)
vim.keymap.set("n", "<C-j>", require("smart-splits").move_cursor_down)
vim.keymap.set("n", "<C-k>", require("smart-splits").move_cursor_up)
vim.keymap.set("n", "<C-l>", require("smart-splits").move_cursor_right)
