local function git(args)
  local out = vim.fn.systemlist(vim.list_extend({ "git" }, args))
  if vim.v.shell_error ~= 0 or out[1] == nil or out[1] == "" then
    return nil
  end
  return out[1]
end

-- Per-repo override (`git config codediff.trunk develop`), then the remote's
-- default branch, then the first of develop/main/master that exists locally.
local function trunk()
  local ref = git({ "config", "--get", "codediff.trunk" })
    or git({ "symbolic-ref", "--short", "refs/remotes/origin/HEAD" })
  if ref then
    return ref
  end
  for _, branch in ipairs({ "develop", "main", "master" }) do
    if git({ "rev-parse", "--verify", "--quiet", branch }) then
      return branch
    end
  end
end

return {
  {
    "esmuellert/codediff.nvim",
    dependencies = { "MunifTanjim/nui.nvim" },
    cmd = "CodeDiff",
    keys = {
      { "gd", false },
      {
        "<leader>goo",
        "<cmd>CodeDiff<cr>",
        desc = "CodeDiff",
      },
      {
        "<leader>got",
        function()
          local ref = trunk()
          if ref then
            vim.cmd("CodeDiff " .. ref)
          else
            vim.notify("No trunk branch found", vim.log.levels.WARN)
          end
        end,
        desc = "CodeDiff trunk",
      },
      {
        "<leader>goc",
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
  {
    "folke/which-key.nvim",
    opts = {
      spec = {
        { "<leader>go", group = "CodeDiff" },
      },
    },
  },
}
