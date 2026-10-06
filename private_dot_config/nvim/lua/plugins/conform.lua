return {
	"stevearc/conform.nvim",
	optional = true,
	opts = {
		formatters_by_ft = {
			python = { "black", "ruff", "ruff_organize_imports" },
			markdown = { "prettier" },
		},
    formatters = {
			prettier = {
				prepend_args = { "--print-width", "120", "--prose-wrap", "always" },
			},
		},
	},
}
