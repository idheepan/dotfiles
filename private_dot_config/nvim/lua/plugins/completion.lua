return {
  {
    "ggml-org/llama.vim",
    init = function()
      vim.g.llama_config = {
        endpoint_fim = "http://localhost:8012/infill",
        auto_fim = true,
        show_info = 2,
        max_line_suffix = 8,

        -- context
        n_prefix = 256,
        n_suffix = 64,
        n_predict = 128,

        -- ring buffer (cross-file context)
        ring_n_chunks = 16,
        ring_chunk_size = 64,
        ring_scope = 1024,
        ring_update_ms = 1000,

        -- timeouts
        t_max_prompt_ms = 500,
        t_max_predict_ms = 3000,

        -- keymaps — these MUST be set or the autocmd errors
        keymap_fim_trigger = "<C-F>",
        keymap_fim_accept_full = "<Tab>",
        keymap_fim_accept_line = "<S-Tab>",
        keymap_fim_accept_word = "<C-B>",
      }
    end,
  },
  {
    "saghen/blink.cmp",
    opts = {
      completion = {
        ghost_text = { enabled = false },
        menu = { auto_show = true },
      },
      sources = {
        default = { "lsp", "path", "buffer" },
      },
    },
  },
}
