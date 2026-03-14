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
# Strip "(1M context)" etc. from model — we already show the context bar
model=$(echo "$model" | sed 's/ ([0-9]*[KMG] context)//')

tokens=$(awk "BEGIN {
  t = $total_input + $total_output
  if (t >= 1000000) printf \"%.1fM\", t/1000000
  else if (t >= 1000) printf \"%.0fk\", t/1000
  else printf \"%d\", t
}")

cost=$(printf '$%.2f' "$cost_raw")

# ──────────────────────────────────────────────────────────────────────
# SECTION: Transcript lookups (cached by mtime)
# Extracts remote_url and compact_count from the JSONL transcript.
# DO NOT REMOVE — provides the remote control URL for line 1 and
# compaction indicators for the context bar.
# ──────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────
# SECTION: Compaction indicator (colored ✻ symbols)
# Shows how many context compactions have occurred in this session.
# DO NOT REMOVE — appended to the context bar, not a standalone part.
# ──────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────
# SECTION: Terminal width
# IMPORTANT: Must be defined BEFORE the context bar and quota bar
# sections, because they use $tw for responsive sizing.
# DO NOT MOVE this below the bar-building sections.
# ──────────────────────────────────────────────────────────────────────
tw=${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}
# Guard against COLUMNS=0 or other tiny values
[ "$tw" -lt 20 ] 2>/dev/null && tw=80

# ──────────────────────────────────────────────────────────────────────
# SECTION: Context bar (visual gauge)
# Shows context window usage as a colored block bar.
# Responsive: 10 blocks at ≥60 cols, 5 blocks when narrower.
# DO NOT REMOVE — this is the primary visual indicator on line 2.
# ──────────────────────────────────────────────────────────────────────
ctx_bar=""
if [ -n "$remaining" ]; then
  ctx_used=$(( 100 - remaining ))
  if [ "$tw" -ge 60 ]; then width=10; danger=2; else width=5; danger=1; fi
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

# ──────────────────────────────────────────────────────────────────────
# SECTION: Usage quota / ammo bar (blue gradient energy bar)
# Shows 5-hour usage quota as a blue gradient ammo countdown.
# Fetches from Anthropic API (cached 60s), falls back to stale data.
# DO NOT REMOVE — this is a key visual feature. If editing nearby
# code, verify the ammo bar still renders at ≥65 col terminals.
# The ammo bar and reset timer are SEPARATE parts so progressive
# fitting can drop the timer first, keeping the ammo visible.
# ──────────────────────────────────────────────────────────────────────
usage_cache="/tmp/claude-usage-cache.json"
usage_ttl=60
quota_bar=""
quota_reset=""
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
    # Responsive: 10 rounds at ≥60 cols, 5 at narrow
    if [ "$tw" -ge 60 ]; then rounds=10; else rounds=5; fi
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

    # Ammo bar and reset timer are SEPARATE line-2 parts.
    # Progressive fitting drops "resets Xm" first, keeping ammo visible.
    quota_bar="${ammo} ${quota_used}%"
    [ -n "$time_str" ] && quota_reset="resets ${time_str}"
  fi
fi

# ──────────────────────────────────────────────────────────────────────
# SECTION: Monitor data sharing (per-session cache files)
# Writes ground-truth context % and remote URL to /tmp/ so the
# claude-monitor TUI can read them instead of estimating.
# DO NOT REMOVE — the monitor depends on these files.
# ──────────────────────────────────────────────────────────────────────
if [ -n "$transcript" ]; then
  _sid=$(echo "$transcript" | sed -n 's|.*/sessions/\([^/]*\)/.*|\1|p')
  if [ -n "$_sid" ]; then
    [ -n "$remaining" ] && printf '%s' "$remaining" > "/tmp/claude-ctx-${_sid}"
    [ -n "$remote_url" ] && printf '%s' "$remote_url" > "/tmp/claude-url-${_sid}"
    [ -n "$session_name" ] && printf '%s' "$session_name" > "/tmp/claude-name-${_sid}"
  fi
fi

echo "$(date '+%H:%M:%S') OK ctx=${remaining:-?} quota=${quota_used:-?} tokens=${tokens}" >> "$SL_LOG"

# ──────────────────────────────────────────────────────────────────────
# SECTION: _vlen — visible character count
# Strips ANSI escape sequences and counts Unicode characters.
# IMPORTANT: Uses perl (not wc -m) because wc -m is locale-dependent
# and counts BYTES instead of characters in pipe contexts without
# UTF-8 locale, inflating lengths and causing parts to be dropped.
# DO NOT replace with wc -m — it will silently break at narrow widths.
# ──────────────────────────────────────────────────────────────────────
_vlen() {
  printf '%b' "$1" | perl -CS -e '
    use utf8;
    local $/; my $s = <STDIN>;
    $s =~ s/\e\[[0-9;]*m//g;
    $s =~ s/\e\]8;;[^\e]*\e\\//g;
    print length($s);
  '
}

# ──────────────────────────────────────────────────────────────────────
# SECTION: Render output
# Line 1: session name + remote control URL (clickable hyperlink)
# Line 2: ctx bar │ tokens │ cost │ model │ ammo │ resets
#
# Progressive fitting: if line 2 is wider than $tw, parts are dropped
# from the RIGHT (lowest priority first):
#   resets → ammo → model → cost → tokens → ctx (never dropped)
#
# IMPORTANT: Line 2 must ALWAYS output something. If all parts are
# dropped, fall back to "ctx XX%". Verify both lines render at widths
# 40, 60, 80, and 100 after ANY change to this section.
#
# DO NOT use wc for measuring — use _vlen (see above).
# DO NOT remove the quota_bar or quota_reset parts — they are the
# blue ammo bar that shows usage quota.
# ──────────────────────────────────────────────────────────────────────
(
  # Line 1: session name (or cwd fallback) + remote control link
  _name="${session_name:-${cwd:-~}}"
  # Truncate session name if wider than half the terminal
  if [ ${#_name} -gt $(( tw / 2 )) ]; then
    _name="${_name:0:$(( tw / 2 - 1 ))}…"
  fi
  if [ -n "$remote_url" ]; then
    # Truncate visible URL to fit terminal width (keep full URL in hyperlink)
    _prefix_len=$(( ${#_name} + 3 ))  # "name │ "
    _url_space=$(( tw - _prefix_len ))
    _display_url="$remote_url"
    if [ "$_url_space" -lt ${#remote_url} ] && [ "$_url_space" -gt 10 ]; then
      _display_url="${remote_url:0:$((_url_space - 1))}…"
    elif [ "$_url_space" -le 10 ]; then
      _display_url=""  # too narrow, hide URL entirely
    fi
    if [ -n "$_display_url" ]; then
      printf '%s %b \033]8;;%s\033\\\033[90m%s\033[0m\033]8;;\033\\\n' "$_name" "$SEP" "$remote_url" "$_display_url"
    else
      printf '%s\n' "$_name"
    fi
  else
    printf '%s %b \033[90mremote control off\033[0m\n' "$_name" "$SEP"
  fi

  # Line 2: ctx bar │ tokens │ cost │ model │ ammo │ resets
  # Parts listed in priority order (rightmost dropped first)
  all_parts=()
  [ -n "$ctx_bar" ] && all_parts+=("$ctx_bar")
  [ -n "$tokens" ] && all_parts+=("${tokens} tok")
  [ -n "$cost" ] && all_parts+=("${cost}")
  [ -n "$model" ] && all_parts+=("$model")
  [ -n "$quota_bar" ] && all_parts+=("$quota_bar")       # ammo gauge — DO NOT REMOVE
  [ -n "$quota_reset" ] && all_parts+=("$quota_reset")    # reset timer — dropped first

  # Progressive fitting: drop rightmost parts until line fits $tw
  parts=("${all_parts[@]}")
  while [ ${#parts[@]} -gt 1 ]; do
    line=""
    for ((i=0; i<${#parts[@]}; i++)); do
      [ $i -gt 0 ] && line+=" │ "
      line+="${parts[$i]}"
    done
    vl=$(_vlen "$line")
    [ "$vl" -le "$tw" ] && break
    unset 'parts[${#parts[@]}-1]'
    parts=("${parts[@]}")  # reindex
  done

  if [ ${#parts[@]} -gt 0 ]; then
    printf '%b' "${parts[0]}"
    for p in "${parts[@]:1}"; do printf ' %b %b' "$SEP" "$p"; done
  fi
  printf '\n'
) 2>/dev/null || printf '%s\n---\n' "${session_name:-${cwd:-~}}"
