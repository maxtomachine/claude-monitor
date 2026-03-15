#!/usr/bin/env bash
SL_LOG="/tmp/claude-statusline.log"
# Keep log from growing unbounded — truncate at 500 lines
if [ -f "$SL_LOG" ] && [ "$(wc -l < "$SL_LOG")" -gt 500 ]; then
  tail -250 "$SL_LOG" > "${SL_LOG}.tmp" && mv "${SL_LOG}.tmp" "$SL_LOG"
fi
echo "$(date '+%H:%M:%S') CALLED pid=$$" >> "$SL_LOG"
input=$(cat)

# ──────────────────────────────────────────────────────────────────────
# SECTION: JSON extraction
# Parses the status JSON from Claude Code into shell variables.
# ──────────────────────────────────────────────────────────────────────
cwd="" session_name="" remaining="" total_input=0 total_output=0 cost_raw=0 model="" transcript="" output_style=""
jq_out=$(echo "$input" | jq -r '
  @sh "cwd=\(.cwd // empty)",
  @sh "session_name=\(.session_name // empty)",
  @sh "remaining=\(.context_window.remaining_percentage // empty)",
  @sh "total_input=\(.context_window.total_input_tokens // 0)",
  @sh "total_output=\(.context_window.total_output_tokens // 0)",
  @sh "cost_raw=\(.cost.total_cost_usd // 0)",
  @sh "model=\(if .model | type == "object" then .model.display_name // .model.id else .model // empty end)",
  @sh "transcript=\(.transcript_path // empty)",
  @sh "output_style=\(.output_style.name // empty)"
' 2>/dev/null) && eval "$jq_out"
cwd=${cwd##*/}
# Strip "(1M context)" etc. from model — we already show the context bar
if [[ "$model" =~ ^(.+)\ \([0-9]+[KMG]\ context\)$ ]]; then
  model="${BASH_REMATCH[1]}"
fi

# Compact token + cost formatting in a single awk call
read -r tokens cost_num <<< $(awk "BEGIN {
  t = $total_input + $total_output
  if (t >= 1000000) printf \"%.1fM \", t/1000000
  else if (t >= 1000) printf \"%.0fk \", t/1000
  else printf \"%d \", t
  c = $cost_raw + 0
  if (c >= 1000) printf \"%.0fk\", c/1000
  else printf \"%.0f\", c
}")

# Fixed-width fields: right-justify number + unit suffix
# "tok" and "   $" align vertically ($ under t)
tok_field=$(printf '%3s tok' "$tokens")
cost_field=$(printf '%3s   $' "$cost_num")

# ──────────────────────────────────────────────────────────────────────
# SECTION: Transcript lookups (cached by mtime)
# Extracts remote_url, compact_count, and effort_level from JSONL.
# ──────────────────────────────────────────────────────────────────────
remote_url=""
compact_count=0
effort_level=""
if [ -n "$transcript" ] && [ -f "$transcript" ]; then
  tcache="/tmp/claude-sl-${transcript//\//_}.cache"
  tcache_stale=true
  if [ -f "$tcache" ]; then
    transcript_mtime=$(stat -f %m "$transcript" 2>/dev/null || echo 0)
    cache_mtime=$(stat -f %m "$tcache" 2>/dev/null || echo 0)
    [ "$cache_mtime" -ge "$transcript_mtime" ] && tcache_stale=false
  fi

  if [ "$tcache_stale" = true ]; then
    _url="" _compact=0 _effort=""
    awk_out=$(awk -F'"' '
      /\"url\":\"https:\/\/claude\.ai\/code\/session_/ {
        for (i=1; i<=NF; i++) {
          if ($i ~ /^https:\/\/claude\.ai\/code\/session_/) { url = $i }
        }
      }
      /\"subtype\":\"compact_boundary\"/ { count++ }
      /[Ee]ffort level/ {
        if (match($0, /effort level to ([a-z]+)/) || match($0, /[Ee]ffort level[: ]+([a-z]+)/)) {
          s = $0
          n = split(s, parts, " to ")
          found = ""
          for (i = 2; i <= n; i++) {
            sub(/[^a-z].*/, "", parts[i])
            if (parts[i] ~ /^(auto|low|medium|high|max)$/) { found = parts[i]; break }
          }
          if (found == "") {
            n = split(s, parts, "level")
            if (n > 1) {
              sub(/^[: ]+/, "", parts[n])
              sub(/[^a-z].*/, "", parts[n])
              if (parts[n] ~ /^(auto|low|medium|high|max)$/) found = parts[n]
            }
          }
          if (found != "") effort = found
        }
      }
      END { printf "_url=%s\n_compact=%d\n_effort=%s\n", url, count+0, effort }
    ' "$transcript" 2>/dev/null) && eval "$awk_out"
    printf '%s\n%s\n%s' "$_url" "$_compact" "$_effort" > "$tcache"
    remote_url="$_url"
    compact_count="$_compact"
    effort_level="$_effort"
  else
    { read -r remote_url; read -r compact_count; read -r effort_level; } < "$tcache"
  fi
  compact_count=${compact_count:-0}
fi

# ──────────────────────────────────────────────────────────────────────
# SECTION: Statusline preferences loader
# Single jq call to read all prefs at once (was 8 separate calls).
# ──────────────────────────────────────────────────────────────────────
sl_show_quota_bar=true
sl_show_fast_mode=true

sl_prefs_out=$(jq -r '
  .statusline // {} |
  @sh "sl_show_quota_bar=\(.quota_bar // true)",
  @sh "sl_show_fast_mode=\(.fast_mode // true)"
' ~/.claude/monitor-prefs.json 2>/dev/null) && eval "$sl_prefs_out"

# ANSI colors
RED="\033[31m"
BRED="\033[1;31m"
CRIT="\033[1;5;31m"
CRIT_BG="\033[1;97;41m"
YEL="\033[33m"
GRN="\033[32m"
ORG="\033[38;5;208m"
DIM="\033[90m"
RST="\033[0m"

# ──────────────────────────────────────────────────────────────────────
# SECTION: Context bar (10-block visual gauge)
# Color tiers: green → yellow → red → bold red → blink red
# ──────────────────────────────────────────────────────────────────────
ctx_bar=""
if [ -n "$remaining" ]; then
  ctx_used=$(( 100 - remaining ))
  width=10; danger=2
  safe=$((width - danger))
  filled=$(( ctx_used * width / 100 ))
  [ "$filled" -gt "$width" ] && filled=$width

  if [ "$ctx_used" -ge 90 ]; then
    color="$CRIT"
  elif [ "$ctx_used" -ge 80 ]; then
    color="$BRED"
  elif [ "$ctx_used" -ge 75 ]; then
    color="$RED"
  elif [ "$ctx_used" -ge 50 ]; then
    color="$YEL"
  else
    color="$GRN"
  fi

  bar=""
  for ((i=0; i<width; i++)); do
    in_danger=$(( i >= safe ))
    if [ "$i" -lt "$filled" ]; then
      if [ "$in_danger" -eq 1 ]; then
        if [ "$ctx_used" -ge 90 ]; then
          bar+="${CRIT}▓${RST}"
        else
          bar+="${RED}▓${RST}"
        fi
      else
        bar+="${color}█${RST}"
      fi
    else
      if [ "$in_danger" -eq 1 ]; then
        bar+="${DIM}▒${RST}"
      else
        bar+="${DIM}░${RST}"
      fi
    fi
  done
  ctx_pct=$(printf '%3d%%' "$ctx_used")
  ctx_bar="ctx ${bar} ${ctx_pct}"
fi

# ──────────────────────────────────────────────────────────────────────
# SECTION: Usage quota / ammo bar (10-block blue gradient)
# Fetches from Anthropic API (cached 60s), falls back to stale data.
# ──────────────────────────────────────────────────────────────────────
usage_cache="/tmp/claude-usage-cache.json"
usage_ttl=60
quota_bar=""
now=$(date +%s)

cache_age=999
if [ -f "$usage_cache" ]; then
  cache_age=$(( now - $(stat -f %m "$usage_cache" 2>/dev/null || echo 0) ))
fi

if [ "$cache_age" -lt "$usage_ttl" ]; then
  usage_json=$(cat "$usage_cache" 2>/dev/null)
else
  token=$(security find-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w 2>/dev/null | jq -r '.claudeAiOauth.accessToken // empty' 2>/dev/null)
  if [ -n "$token" ]; then
    usage_json=$(curl -s --max-time 3 -H "Authorization: Bearer $token" -H "anthropic-beta: oauth-2025-04-20" "https://api.anthropic.com/api/oauth/usage" 2>/dev/null)
    if echo "$usage_json" | jq -e '.five_hour' >/dev/null 2>&1; then
      echo "$usage_json" > "$usage_cache"
    else
      usage_json=$(cat "$usage_cache" 2>/dev/null)
    fi
  fi
fi

if [ -n "$usage_json" ]; then
  five_hour_pct="" five_hour_reset="" _extra_used=""
  usage_jq_out=$(echo "$usage_json" | jq -r '
    @sh "five_hour_pct=\(.five_hour.utilization // empty)",
    @sh "five_hour_reset=\(.five_hour.resets_at // empty)",
    @sh "_extra_used=\(.extra_usage.used_credits // empty)"
  ' 2>/dev/null) && eval "$usage_jq_out"

  if [ -n "$five_hour_pct" ]; then
    quota_used=$(printf '%.0f' "$five_hour_pct")

    rounds=10
    spent=$(( quota_used * rounds / 100 ))
    [ "$spent" -gt "$rounds" ] && spent=$rounds
    ammo_remaining=$(( rounds - spent ))
    blue_shades=(39 38 33 27 26 21 20 19 18 17)

    ammo=""
    for ((i=0; i<rounds; i++)); do
      if [ "$i" -lt "$ammo_remaining" ]; then
        grad_idx=$(( (ammo_remaining - 1 - i) * 9 / (ammo_remaining > 1 ? ammo_remaining - 1 : 1) ))
        [ "$grad_idx" -gt 9 ] && grad_idx=9
        ammo+="\033[38;5;${blue_shades[$grad_idx]}m▮${RST}"
      else
        ammo+="${DIM}▯${RST}"
      fi
    done

    quota_pct=$(printf '%3d%%' "$quota_used")
    quota_bar="${ammo} ${quota_pct}"
  fi
fi

# ──────────────────────────────────────────────────────────────────────
# SECTION: Fast mode + extra billing
# ──────────────────────────────────────────────────────────────────────
fast_indicator=""
extra_cost=""
is_fast=false
if [ "$output_style" = "fast" ]; then
  is_fast=true
  fast_indicator="${ORG}⚡ fast${RST}"
fi
if [ -n "$_extra_used" ] && [ "$_extra_used" != "null" ] && [ "$_extra_used" != "0" ]; then
  extra_cost=$(printf '$%.2f' "$_extra_used")
fi

# ──────────────────────────────────────────────────────────────────────
# SECTION: Effort level indicator
# Parsed from transcript. Only shown when not "auto" (default).
# ──────────────────────────────────────────────────────────────────────
effort_indicator=""
if [ -n "$effort_level" ] && [ "$effort_level" != "auto" ]; then
  case "$effort_level" in
    max)    effort_indicator="🧠 \033[38;5;175m max${RST}" ;;
    high)   effort_indicator="🧠 ${GRN}high${RST}" ;;
    medium) effort_indicator="🧠 ${YEL} med${RST}" ;;
    low)    effort_indicator="🧠 ${DIM} low${RST}" ;;
    *)      effort_indicator="🧠 ${DIM} ${effort_level}${RST}" ;;
  esac
fi

# ──────────────────────────────────────────────────────────────────────
# SECTION: Monitor data sharing (per-session cache files)
# Writes ground-truth data to /tmp/ for claude-monitor TUI.
# ──────────────────────────────────────────────────────────────────────
if [ -n "$transcript" ]; then
  _sid=$(basename "$transcript" .jsonl)
  [[ "$_sid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || _sid=""
  if [ -n "$_sid" ]; then
    [ -n "$remaining" ] && printf '%s' "$remaining" > "/tmp/claude-ctx-${_sid}"
    [ -n "$remote_url" ] && printf '%s' "$remote_url" > "/tmp/claude-url-${_sid}"
    [ -n "$session_name" ] && printf '%s' "$session_name" > "/tmp/claude-name-${_sid}"
    [ -n "$cost_raw" ] && [ "$cost_raw" != "0" ] && printf '%s' "$cost_raw" > "/tmp/claude-cost-${_sid}"
    [ "$is_fast" = true ] && printf '%s' "$extra_cost" > "/tmp/claude-fast-${_sid}"
  fi
fi

echo "$(date '+%H:%M:%S') OK ctx=${remaining:-?} quota=${quota_used:-?} tokens=${tokens}" >> "$SL_LOG"

# ──────────────────────────────────────────────────────────────────────
# SECTION: Render output
# Two-line mini HUD. No truncation — terminal clips naturally.
# Order (left to right): bar  %  indicator  metrics
# Most important info is leftmost, visible at any width.
# ──────────────────────────────────────────────────────────────────────
(
  # ── Line 1: ctx bar + effort + tokens ──
  if [ -n "$ctx_bar" ]; then
    line1="$ctx_bar"
    if [ -n "$effort_indicator" ]; then
      line1="${line1}  ${effort_indicator}"
    elif [ "$is_fast" = true ]; then
      line1="${line1}          "
    fi
    line1="${line1}   ${DIM}${tok_field}${RST}"
    printf '%b\n' "$line1"
  else
    printf '\n'
  fi

  # ── Line 2: use bar + fast + cost ──
  if [ "$sl_show_quota_bar" = true ] && [ -n "$quota_bar" ]; then
    line2="use ${quota_bar}"
    if [ "$is_fast" = true ] && [ "$sl_show_fast_mode" = true ]; then
      line2="${line2}  ${fast_indicator}"
      [ -n "$extra_cost" ] && line2="${line2} ${YEL}+${extra_cost}${RST}"
    elif [ -n "$effort_indicator" ]; then
      line2="${line2}         "
    fi
    line2="${line2}   ${DIM}${cost_field}${RST}"
    printf '%b\n' "$line2"
  else
    printf '\n'
  fi
) 2>/dev/null || printf 'statusline error\n'
