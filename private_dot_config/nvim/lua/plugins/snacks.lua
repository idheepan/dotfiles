return {
  {
    "folke/snacks.nvim",
    opts = {
      dashboard = { enabled = false },

      explorer = {
        enabled = false,
      },
    },
    keys = {
      { "<leader>/", LazyVim.pick("live_grep", { root = false }), desc = "Grep (cwd)" },
      { "<leader><space>", LazyVim.pick("files", { root = false }), desc = "Find Files (cwd)" },
      {
        "g-", -- "Go Directory"
        function()
          -- Use the general 'files' picker but filter for directories
          Snacks.picker.files({
            cmd = "fd",
            args = { "--type", "d", "--hidden", "--exclude", ".git" },
            -- What happens when you press Enter
            confirm = function(picker, item)
              picker:close()
              -- item.file is the path to the directory found
              require("oil").open(item.file)
            end,
            -- Optional: A title to remind you what you're looking for
            title = "Find Directory",
          })
        end,
        desc = "Find Directory (Oil)",
      },
    },
  },
}
