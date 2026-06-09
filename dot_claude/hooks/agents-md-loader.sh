AGENTS_FILE=""
for f in ./AGENTS.md ./agents.md ./.agents.md; do
  [ -f "$f" ] && AGENTS_FILE="$f" && break
done
[ -z "$AGENTS_FILE" ] && exit 0
CONTENT=$(cat "$AGENTS_FILE")
[ -z "$CONTENT" ] && exit 0
jq -n --arg ctx "Instructions from AGENTS.md:
$CONTENT" '{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": $ctx
  }
}'
exit 0
