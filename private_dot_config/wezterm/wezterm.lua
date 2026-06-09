-- Pull in the wezterm API
local wezterm = require("wezterm")
local smart_splits = wezterm.plugin.require("https://github.com/mrjones2014/smart-splits.nvim")

-- This will hold the configuration.
local config = wezterm.config_builder()

-- This is where you actually apply your config choices.

-- For example, changing the initial geometry for new windows:
config.initial_cols = 120
config.initial_rows = 28

-- font size and color scheme.
config.font_size = 10
config.font = wezterm.font_with_fallback({ "Iosevka Nerd Font", "Noto Color Emoji" })
config.color_scheme = "Catppuccin Mocha"
-- Catppuccin Mocha Colors
local catppuccin_mocha = {
	base = "#1e1e2e",
	mantle = "#181825",
	crust = "#11111b",
	surface0 = "#313244",
	surface1 = "#45475a",
	surface2 = "#585b70",
	text = "#cdd6f4",
	subtext0 = "#a6adc8",
	blue = "#89b4fa",
	sky = "#89dceb",
	lavender = "#b4befe",
}
config.window_decorations = "NONE"
config.window_close_confirmation = "NeverPrompt"
config.hide_tab_bar_if_only_one_tab = true
config.adjust_window_size_when_changing_font_size = false

config.keys = require("keys")
config.ssh_domains = require("ssh_domains")
config.unix_domains = require("unix_domains")
-- you can put the rest of your Wezterm config here
smart_splits.apply_to_config(config, {
	-- the default config is here, if you'd like to use the default keys,
	-- you can omit this configuration table parameter and just use
	-- smart_splits.apply_to_config(config)

	-- directional keys to use in order of: left, down, up, right
	direction_keys = { "h", "j", "k", "l" },
	-- if you want to use separate direction keys for move vs. resize, you
	-- can also do this:
	-- direction_keys = {
	-- 	move = { "h", "j", "k", "l" },
	-- 	resize = { "LeftArrow", "DownArrow", "UpArrow", "RightArrow" },
	-- },
	-- modifier keys to combine with direction_keys
	modifiers = {
		move = "CTRL", -- modifier to use for pane movement, e.g. CTRL+h to move left
		resize = "META", -- modifier to use for pane resize, e.g. META+h to resize to the left
	},
})

-- tab bar style
config.window_frame = {
	font_size = 10,
}

-- This function returns the suggested title for a tab.
-- It prefers the title that was set via `tab:set_title()`
-- or `wezterm cli set-tab-title`, but falls back to the
-- title of the active pane in that tab.
local function tab_title(tab_info)
	local title = tab_info.tab_title
	-- if the tab title is explicitly set, take that
	if title and #title > 0 then
		return title
	end
	-- Otherwise, use the title from the active pane
	-- in that tab
	return tab_info.active_pane.title
end

wezterm.on("format-tab-title", function(tab, _, _, _, _, max_width)
	local background = catppuccin_mocha.base
	local foreground = catppuccin_mocha.surface1

	if tab.is_active then
		foreground = catppuccin_mocha.sky
	end

	local edge_foreground = background

	local title = tab_title(tab)

	-- ensure that the titles fit in the available space,
	-- and that we have room for the edges.
	title = wezterm.truncate_right(title, max_width - 2)

	return {
		-- { Background = { Color = edge_background } },
		-- { Foreground = { Color = edge_foreground } },
		-- -- { Text = SOLID_LEFT_ARROW },
		{ Background = { Color = background } },
		{ Foreground = { Color = foreground } },
		{ Text = "  " .. tab.tab_index + 1 .. "    " .. title .. "  " },
		{ Background = { Color = edge_foreground } },
		{ Foreground = { Color = foreground } },
		-- { Text = SLASH },
		-- { Background = { Color = edge_background } },
		-- { Foreground = { Color = edge_foreground } },
		-- { Text = SOLID_RIGHT_ARROW },
	}
end)
-- Finally, return the configuration to wezterm:
return config
