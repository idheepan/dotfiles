return {
  "neovim/nvim-lspconfig",
  opts = {
    servers = {
      svelte = {
        settings = {
          inlayHints = {
            enumMemberValues = {
              enabled = true,
            },
            functionLikeReturnTypes = {
              enabled = true,
            },
            parameterNames = {
              enabled = "literals",
              suppressWhenArgumentMatchesName = true,
            },
            parameterTypes = {
              enabled = true,
            },
            propertyDeclarationTypes = {
              enabled = true,
            },
            variableTypes = {
              enabled = true,
            },
          },
        },
      },
      rust_analyzer = {
        settings = {
          ["rust-analyzer"] = {
            inlayHints = {
              lifetimeElisionHints = {
                enable = "always",
                useParameterNames = true,
              },
            },
          },
        },
      },
      -- Add tinymist configuration
      tinymist = {
        -- Use Mason-installed tinymist
        mason = true,
        settings = {
          formatterMode = "typstyle", -- Use typstyle for formatting
          exportPdf = "onSave", -- Auto-export PDF on save (or "onType")
        },

        -- Custom keymaps for Typst
        keys = {
          {
            "<leader>tp",
            function()
              local client = vim.lsp.get_clients({ name = "tinymist", bufnr = 0 })[1]
              if client then
                client:exec_cmd({
                  title = "Pin current Typst file",
                  command = "tinymist.pinMain",
                  arguments = { vim.api.nvim_buf_get_name(0) },
                }, { bufnr = 0 })
              end
            end,
            desc = "Pin main Typst file",
            ft = "typst",
          },
          {
            "<leader>tu",
            function()
              local client = vim.lsp.get_clients({ name = "tinymist", bufnr = 0 })[1]
              if client then
                client:exec_cmd({
                  title = "Unpin Main File", -- Added missing field
                  command = "tinymist.pinMain",
                  arguments = { vim.v.null },
                }, { bufnr = 0 })
              end
            end,
            desc = "Unpin Typst main file",
            ft = "typst",
          },
        },
      },
    },
  },
}
