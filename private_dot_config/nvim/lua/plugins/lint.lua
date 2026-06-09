return {
  "mfussenegger/nvim-lint",
  opts = {
    linters = {
      ["markdownlint-cli2"] = {
        stdin = false,
        args = {},
        parser = require("lint.parser").from_errorformat(
          "%f:%l:%c %m,%f:%l %m",
          { source = "markdownlint", severity = vim.diagnostic.severity.WARN }
        ),
      },
    },
  },
}
