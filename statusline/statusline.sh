#!/usr/bin/env bash
SL_LOG="/tmp/claude-statusline.log"
# Keep log from growing unbounded — truncate at 500 lines
if [ -f "$SL_LOG" ] && [ "$(wc -l < "$SL_LOG")" -gt 500 ]; then
  tail -250 "$SL_LOG" > "${SL_LOG}.tmp" && mv "${SL_LOG}.tmp" "$SL_LOG"
fi
echo "$(date '+%H:%M:%S') CALLED pid=$$" >> "$SL_LOG"
input=$(cat)

# Single jq call to extract all fields — wrapped so parse errors don't kill statusline
cwd="" session_name="" remaining="" total_input=0 total_output=0 cost_raw=0 model="" transcript=""
jq_out=$(echo "$input" | jq -r '
  @sh "cwd=\(.cwd // empty)",
  @sh "session_name=\(.session_name // empty)",
  @sh "remaining=\(.context_window.remaining_percentage // empty)",
  @sh "total_input=\(.context_window.total_input_tokens // 0)",
  @sh "total_output=\(.context_window.total_output_tokens // 0)",
  @sh "cost_raw=\(.cost.total_cost_usd // 0)",
  @sh "model=\(if .model | type == "object" then .model.display_name // .model.id else .model // empty end)",
  @sh "transcript=\(.transcript_path // empty)"
' 2>/dev/null) && eval "$jq_out"
cwd=${cwd##*/}

tokens=$(awk "BEGIN {
  t = $total_input + $total_output
  if (t >= 1000000) printf \"%.1fM\", t/1000000
  else if (t >= 1000) printf \"%.0fk\", t/1000
  else printf \"%d\", t
}")

cost=$(printf '$%.2f' "$cost_raw")

# Transcript lookups — cached by transcript mtime to avoid grepping large JSONL every render
remote_url=""
compact_count=0
if [ -n "$transcript" ] && [ -f "$transcript" ]; then
  tcache="/tmp/claude-sl-${transcript//\//_}.cache"
  tcache_stale=true
  if [ -f "$tcache" ]; then
    transcript_mtime=$(stat -f %m "$transcript" 2>/dev/null || echo 0)
    cache_mtime=$(stat -f %m "$tcache" 2>/dev/null || echo 0)
    [ "$cache_mtime" -ge "$transcript_mtime" ] && tcache_stale=false
  fi

  if [ "$tcache_stale" = true ]; then
    # Single awk pass over transcript instead of two greps
    _url="" _compact=0
    awk_out=$(awk -F'"' '
      /\"url\":\"https:\/\/claude\.ai\/code\/session_/ {
        for (i=1; i<=NF; i++) {
          if ($i ~ /^https:\/\/claude\.ai\/code\/session_/) { url = $i }
        }
      }
      /\"subtype\":\"compact_boundary\"/ { count++ }
      END { printf "_url=%s\n_compact=%d\n", url, count+0 }
    ' "$transcript" 2>/dev/null) && eval "$awk_out"
    printf '%s\n%s' "$_url" "$_compact" > "$tcache"
    remote_url="$_url"
    compact_count="$_compact"
  else
    { read -r remote_url; read -r compact_count; } < "$tcache"
  fi
  compact_count=${compact_count:-0}
fi

# ANSI colors
RED="\033[31m"
YEL="\033[33m"
GRN="\033[32m"
ORG="\033[38;5;208m"
DIM="\033[90m"
SEP="\033[38;5;238m│\033[0m"
RST="\033[0m"

# Compaction indicator: colored ✻ symbols
compact_str=""
if [ "$compact_count" -gt 0 ]; then
  if [ "$compact_count" -ge 5 ]; then
    compact_str="${RED}"
  elif [ "$compact_count" -ge 4 ]; then
    compact_str="${ORG}"
  elif [ "$compact_count" -ge 3 ]; then
    compact_str="${YEL}"
  else
    compact_str="${GRN}"
  fi
  for ((i=0; i<compact_count && i<5; i++)); do
    compact_str+="✻"
  done
  [ "$compact_count" -gt 5 ] && compact_str+="(${compact_count})"
  compact_str+="${RST}"
fi

# Context bar with danger zone
# 10 blocks total. Last 2 = danger zone (15% ~ auto-compact)
ctx_bar=""
if [ -n "$remaining" ]; then
  ctx_used=$(( 100 - remaining ))
  width=10
  danger=2
  safe=$((width - danger))
  filled=$(( ctx_used * width / 100 ))
  [ "$filled" -gt "$width" ] && filled=$width

  if [ "$ctx_used" -ge 75 ]; then
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
        bar+="${RED}▓${RST}"
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
  ctx_bar="ctx ${bar} ${ctx_used}%"
  [ -n "$compact_str" ] && ctx_bar="${ctx_bar} ${compact_str}"
fi

# Usage quota (cached for 60s, stale fallback on API failure)
usage_cache="/tmp/claude-usage-cache.json"
usage_ttl=60
quota_bar=""
now=$(date +%s)
stale_usage=$(cat "$usage_cache" 2>/dev/null)

cache_age=999
if [ -f "$usage_cache" ]; then
  cache_age=$(( now - $(stat -f %m "$usage_cache" 2>/dev/null || echo 0) ))
fi

if [ "$cache_age" -lt "$usage_ttl" ]; then
  usage_json="$stale_usage"
else
  # jq instead of python3 for keychain JSON extraction
  token=$(security find-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w 2>/dev/null | jq -r '.claudeAiOauth.accessToken // empty' 2>/dev/null)
  if [ -n "$token" ]; then
    usage_json=$(curl -s --max-time 3 -H "Authorization: Bearer $token" -H "anthropic-beta: oauth-2025-04-20" "https://api.anthropic.com/api/oauth/usage" 2>/dev/null)
    if echo "$usage_json" | jq -e '.five_hour' >/dev/null 2>&1; then
      echo "$usage_json" > "$usage_cache"
    else
      usage_json="$stale_usage"
    fi
  else
    usage_json="$stale_usage"
  fi
fi

if [ -n "$usage_json" ]; then
  # Single jq call for both fields — fail-safe
  five_hour_pct="" five_hour_reset=""
  usage_jq_out=$(echo "$usage_json" | jq -r '
    @sh "five_hour_pct=\(.five_hour.utilization // empty)",
    @sh "five_hour_reset=\(.five_hour.resets_at // empty)"
  ' 2>/dev/null) && eval "$usage_jq_out"

  if [ -n "$five_hour_pct" ]; then
    quota_used=$(printf '%.0f' "$five_hour_pct")
    time_str=""
    if [ -n "$five_hour_reset" ]; then
      # Handle both +00:00 and Z timezone suffixes
      clean_ts=$(echo "$five_hour_reset" | sed 's/\.[0-9]*//;s/Z$/+00:00/;s/+00:00//')
      reset_epoch=$(date -jf "%Y-%m-%dT%H:%M:%S" "$clean_ts" +%s 2>/dev/null)
      if [ -n "$reset_epoch" ]; then
        mins_left=$(( (reset_epoch - now) / 60 ))
        if [ "$mins_left" -gt 60 ]; then
          time_str="$(( mins_left / 60 ))h$(( mins_left % 60 ))m"
        elif [ "$mins_left" -gt 0 ]; then
          time_str="${mins_left}m"
        else
          time_str="now"
        fi
      fi
    fi

    # Ammo countdown: blue gradient energy bar (dark left → bright right)
    rounds=10
    spent=$(( quota_used * rounds / 100 ))
    [ "$spent" -gt "$rounds" ] && spent=$rounds
    ammo_remaining=$(( rounds - spent ))
    blue_shades=(39 38 33 27 26 21 20 19 18 17)

    ammo=""
    for ((i=0; i<rounds; i++)); do
      if [ "$i" -lt "$ammo_remaining" ]; then
        # Gradient index: maps position to 10-shade blue palette (0=brightest, 9=darkest)
        grad_idx=$(( (ammo_remaining - 1 - i) * 9 / (ammo_remaining > 1 ? ammo_remaining - 1 : 1) ))
        [ "$grad_idx" -gt 9 ] && grad_idx=9
        ammo+="\033[38;5;${blue_shades[$grad_idx]}m▮${RST}"
      else
        ammo+="${DIM}▯${RST}"
      fi
    done

    quota_bar="quota ${ammo} ${quota_used}%"
    [ -n "$time_str" ] && quota_bar="${quota_bar} resets ${time_str}"
  fi
fi

echo "$(date '+%H:%M:%S') OK ctx=${remaining:-?} quota=${quota_used:-?} tokens=${tokens}" >> "$SL_LOG"

# Render output — subshell so any failure emits fallback instead of nothing
(
  # Line 1: session name (or cwd fallback) + remote control link
  if [ -n "$remote_url" ]; then
    printf '%s %b \033]8;;%s\033\\\033[90m%s\033[0m\033]8;;\033\\\n' "${session_name:-${cwd:-~}}" "$SEP" "$remote_url" "$remote_url"
  else
    printf '%s %b \033[90mremote control off\033[0m\n' "${session_name:-${cwd:-~}}" "$SEP"
  fi

  # Line 2: ctx bar | quota ammo | tokens | cost | model
  parts=()
  [ -n "$ctx_bar" ] && parts+=("$ctx_bar")
  [ -n "$quota_bar" ] && parts+=("$quota_bar")
  [ -n "$tokens" ] && parts+=("${tokens} tok")
  [ -n "$cost" ] && parts+=("${cost}")
  [ -n "$model" ] && parts+=("$model")

  if [ ${#parts[@]} -gt 0 ]; then
    printf '%b' "${parts[0]}"
    for p in "${parts[@]:1}"; do printf ' %b %b' "$SEP" "$p"; done
  fi
  printf '\n'
) 2>/dev/null || printf '%s\n---\n' "${session_name:-${cwd:-~}}"