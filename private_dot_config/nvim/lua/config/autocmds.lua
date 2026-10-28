-- Autocmds are automatically loaded on the VeryLazy event
-- Default autocmds that are always set: https://github.com/LazyVim/LazyVim/blob/main/lua/lazyvim/config/autocmds.lua
--
-- Add any additional autocmds here
-- with `vim.api.nvim_create_autocmd`
--
-- Or remove existing autocmds by their group name (which is prefixed with `lazyvim_` for the defaults)
-- e.g. vim.api.nvim_del_augroup_by_name("lazyvim_wrap_spell")

-- Publish the file being edited as this tmux pane's @status-center option,
-- which tmux.conf draws in the middle of the status bar. Tabs are hidden, so
-- this is where to look for the current file. Cleared on exit and suspend so
-- the middle is empty for panes that aren't running nvim.
if vim.env.TMUX and vim.env.TMUX_PANE then
  local published

  local function publish(text, sync)
    if text == published then
      return
    end
    published = text
    local cmd = { "tmux", "set-option", "-p", "-t", vim.env.TMUX_PANE, "@status-center" }
    if text then
      table.insert(cmd, text)
    else
      table.insert(cmd, 4, "-u")
    end
    local job = vim.system(cmd)
    if sync then
      job:wait(500)
    end
  end

  -- Nil means "leave it alone", so floats (pickers, hovers) and special buffers
  -- like terminals and help don't replace the file underneath them.
  local function current_file()
    if vim.api.nvim_win_get_config(0).relative ~= "" then
      return nil
    end
    local name = vim.api.nvim_buf_get_name(0)
    local is_oil = name:match("^oil://") ~= nil
    if vim.bo.buftype ~= "" and not is_oil then
      return nil
    end
    if name == "" then
      return "[No Name]"
    end
    local text = vim.fn.fnamemodify(name:gsub("^oil://", ""), ":~:.")
    if text == "" then
      text = "./"
    end
    if vim.bo.modified then
      text = text .. " [+]"
    end
    return text
  end

  local function update()
    local text = current_file()
    if text then
      publish(text)
    end
  end

  local group = vim.api.nvim_create_augroup("tmux_status_center", { clear = true })
  vim.api.nvim_create_autocmd(
    { "BufEnter", "WinEnter", "BufFilePost", "BufModifiedSet", "DirChanged" },
    { group = group, callback = update }
  )
  vim.api.nvim_create_autocmd("VimResume", {
    group = group,
    callback = function()
      published = false
      update()
    end,
  })
  vim.api.nvim_create_autocmd({ "VimSuspend", "VimLeavePre" }, {
    group = group,
    callback = function()
      publish(nil, true)
    end,
  })

  -- This file loads on VeryLazy, after the first BufEnter has already fired.
  update()
end
