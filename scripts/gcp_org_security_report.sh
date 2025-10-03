#!/usr/bin/env bash
set -euo pipefail

# Google Cloud org security report (HTML) using REST APIs, no gcloud
# - Auth: Access token (env/flag) or Service Account key JSON (JWT flow)
# - Data: Project count (CRM v3), assets outside AU (CAI), SECURITY recommendations (Recommender)
# - Output: HTML file suitable for emailing

################################################################################
# Globals and defaults
################################################################################
SCRIPT_NAME="$(basename "$0")"
ORG_ID=""
SA_KEY_FILE=""
ACCESS_TOKEN=""
OUTPUT_FILE="./gcp_org_security_report.html"
ALLOWED_AU_LOCATIONS="australia-southeast1,australia-southeast2"
HTTP_TIMEOUT=60
CURL_FLAGS=(--silent --show-error --fail --max-time "$HTTP_TIMEOUT")
WORK_DIR=""
VERBOSE=0
LOG_TO_STDOUT=0
SKIP_CAI=0
SKIP_RECOMMENDER=0
PROJECTS_LIMIT=0

################################################################################
# Utilities
################################################################################
log() {
  local line
  line="[$(date +'%Y-%m-%dT%H:%M:%S%z')] $*"
  printf "%s\n" "$line" >&2
  if [[ "${LOG_TO_STDOUT:-0}" -eq 1 ]]; then
    printf "%s\n" "$line"
  fi
}
fail() { log "ERROR: $*"; exit 1; }

have_cmd() { command -v "$1" >/dev/null 2>&1; }

b64_raw() {
  # base64 without newlines, fallback to openssl
  if have_cmd base64; then
    if base64 --help 2>&1 | grep -q -- "--wrap"; then
      base64 --wrap=0
    else
      base64 | tr -d '\n'
    fi
  else
    openssl base64 -A
  fi
}

b64url() {
  # base64url (RFC 4648, no padding)
  b64_raw | tr '+/' '-_' | tr -d '='
}

json_escape() {
  # Escape arbitrary text as JSON string using jq
  jq -R -s '.'
}

mkwork() {
  WORK_DIR="$(mktemp -d)"
  trap 'rm -rf "$WORK_DIR"' EXIT
}

require_tools() {
  local tools=(curl jq)
  for t in "${tools[@]}"; do
    have_cmd "$t" || fail "Missing required tool: $t"
  done
  # openssl only required for SA key based auth
  if [[ -n "$SA_KEY_FILE" && -z "${ACCESS_TOKEN:-}" ]]; then
    have_cmd openssl || fail "openssl is required for service account JWT auth"
  fi
}

usage() {
  cat <<EOF
$SCRIPT_NAME - Generate GCP org security report (HTML) via REST APIs

Usage:
  $SCRIPT_NAME --org-id ORG_ID [options]

Required:
  --org-id ORG_ID                 Organization numeric ID (e.g., 123456789012)

Auth (one of):
  --token ACCESS_TOKEN            Pre-fetched OAuth2 access token (cloud-platform)
  --sa-key-file PATH              Service Account key JSON file to mint access token

Options:
  --output-file PATH              Output HTML file path (default: ./gcp_org_security_report.html)
  --au-locations CSV              Allowed AU locations list (default: australia-southeast1,australia-southeast2)
  --timeout SECONDS               HTTP timeout per request (default: 60)
  --verbose                       Enable verbose execution trace
  --log-stdout                    Duplicate logs to stdout (in addition to stderr)
  --skip-cai                      Skip Cloud Asset Inventory section
  --skip-recommender              Skip Active Assist recommender section
  --projects-limit N              Only process first N projects for recommender
  -h, --help                      Show this help

Notes:
  - Requires APIs enabled: Cloud Resource Manager v3, Cloud Asset Inventory, Recommender
  - Requires permissions: resourcemanager.projectViewer (or above), cloudasset.viewer, recommender.viewer
  - No gcloud used; pure REST via curl
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --org-id) ORG_ID="$2"; shift 2 ;;
      --sa-key-file) SA_KEY_FILE="$2"; shift 2 ;;
      --token) ACCESS_TOKEN="$2"; shift 2 ;;
      --output-file) OUTPUT_FILE="$2"; shift 2 ;;
      --au-locations) ALLOWED_AU_LOCATIONS="$2"; shift 2 ;;
      --timeout) HTTP_TIMEOUT="$2"; CURL_FLAGS=(--silent --show-error --fail --max-time "$HTTP_TIMEOUT"); shift 2 ;;
      --verbose) VERBOSE=1; shift 1 ;;
      --log-stdout) LOG_TO_STDOUT=1; shift 1 ;;
      --skip-cai) SKIP_CAI=1; shift 1 ;;
      --skip-recommender) SKIP_RECOMMENDER=1; shift 1 ;;
      --projects-limit) PROJECTS_LIMIT="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) fail "Unknown argument: $1" ;;
    esac
  done

  [[ -z "$ORG_ID" ]] && fail "--org-id is required"
  if [[ "$VERBOSE" -eq 1 ]]; then
    set -x
  fi
}

################################################################################
# Auth: Access token
################################################################################
get_access_token_from_sa() {
  local key_file="$1"
  [[ -f "$key_file" ]] || fail "Service account key not found: $key_file"

  local client_email token_uri private_key
  client_email=$(jq -r '.client_email' < "$key_file")
  token_uri=$(jq -r '.token_uri // "https://oauth2.googleapis.com/token"' < "$key_file")
  private_key=$(jq -r '.private_key' < "$key_file")
  [[ -n "$client_email" && -n "$private_key" ]] || fail "Invalid service account key JSON"

  local scope="https://www.googleapis.com/auth/cloud-platform"
  local now exp
  now=$(date +%s)
  exp=$((now + 3600))

  local header payload signed assertion
  header='{"alg":"RS256","typ":"JWT"}'
  payload=$(jq -n --arg iss "$client_email" \
                 --arg sub "$client_email" \
                 --arg aud "$token_uri" \
                 --arg scope "$scope" \
                 --argjson iat "$now" \
                 --argjson exp "$exp" '{iss:$iss,sub:$sub,aud:$aud,scope:$scope,iat:$iat,exp:$exp}')

  local header_b64 payload_b64
  header_b64=$(printf '%s' "$header" | b64url)
  payload_b64=$(printf '%s' "$payload" | b64url)

  local signing_input
  signing_input="$header_b64.$payload_b64"

  # Write key to a temp file to avoid escaping issues
  local key_pem="$WORK_DIR/sa_key.pem"
  printf '%s\n' "$private_key" > "$key_pem"

  local signature_b64url
  signature_b64url=$(printf '%s' "$signing_input" | openssl dgst -sha256 -sign "$key_pem" -binary | b64url)

  assertion="$signing_input.$signature_b64url"

  local resp
  resp=$(curl "${CURL_FLAGS[@]}" -X POST \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=$assertion" \
    "$token_uri") || fail "Failed to obtain access token from SA key"

  printf '%s' "$resp" | jq -r '.access_token // empty' | grep -q '.' || fail "Token response missing access_token"
  printf '%s' "$resp" | jq -r '.access_token'
}

ensure_access_token() {
  if [[ -n "${ACCESS_TOKEN:-}" ]]; then
    return
  fi
  if [[ -n "$SA_KEY_FILE" ]]; then
    log "Minting access token using service account key"
    ACCESS_TOKEN=$(get_access_token_from_sa "$SA_KEY_FILE")
    return
  fi
  # Fallback to env var if set
  if [[ -n "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ]]; then
    ACCESS_TOKEN="$GOOGLE_OAUTH_ACCESS_TOKEN"
    return
  fi
  fail "No authentication provided. Use --token or --sa-key-file"
}

auth_header() { printf 'Authorization: Bearer %s' "$ACCESS_TOKEN"; }

################################################################################
# API helpers
################################################################################
api_get() {
  local url="$1"
  curl "${CURL_FLAGS[@]}" -H "$(auth_header)" -H 'Accept: application/json' "$url"
}

api_post_json() {
  local url="$1"; shift
  local body="$1"; shift || true
  curl "${CURL_FLAGS[@]}" -X POST -H "$(auth_header)" -H 'Content-Type: application/json' -d "$body" "$url"
}

################################################################################
# Data fetchers
################################################################################
fetch_projects() {
  # Returns JSON array of projects with id and number
  local url="https://cloudresourcemanager.googleapis.com/v3/projects:search"
  local query="parent.type:organization parent.id:${ORG_ID} state:ACTIVE"
  local pageToken=""
  local all="[]"

  while :; do
    local req
    if [[ -n "$pageToken" ]]; then
      req=$(jq -n --arg q "$query" --arg pt "$pageToken" '{query:$q,pageSize:1000,pageToken:$pt}')
    else
      req=$(jq -n --arg q "$query" '{query:$q,pageSize:1000}')
    fi
    local resp
    resp=$(api_post_json "$url" "$req") || fail "Fetching projects failed"

    local items token
    items=$(printf '%s' "$resp" | jq -c '.projects // []')
    all=$(jq -c --argjson a "$all" --argjson b "$items" '$a + $b' <<<"null")

    token=$(printf '%s' "$resp" | jq -r '.nextPageToken // empty')
    if [[ -z "$token" ]]; then break; fi
    pageToken="$token"
  done

  printf '%s' "$all" | jq -c '[.[] | {projectId:.projectId, projectNumber:(.projectNumber|tostring), displayName:.displayName}]'
}

fetch_cai_outside_au() {
  # Returns JSON array of resources outside allowed AU locations
  local scope="organizations/${ORG_ID}"
  local url="https://cloudasset.googleapis.com/v1/${scope}:searchAllResources"
  local pageToken=""
  local all="[]"

  while :; do
    local req
    if [[ -n "$pageToken" ]]; then
      req=$(jq -n --arg pt "$pageToken" '{pageSize:500,pageToken:$pt}')
    else
      req=$(jq -n '{pageSize:500}')
    fi
    local resp
    resp=$(api_post_json "$url" "$req") || {
      log "WARN: Cloud Asset Inventory search failed (is API enabled and permissions granted?). Skipping CAI section.";
      printf '[]'; return 0;
    }

    local items token
    items=$(printf '%s' "$resp" | jq -c '.results // []')
    all=$(jq -c --argjson a "$all" --argjson b "$items" '$a + $b' <<<"null")

    token=$(printf '%s' "$resp" | jq -r '.nextPageToken // empty')
    if [[ -z "$token" ]]; then break; fi
    pageToken="$token"
  done

  # Filter out resources whose location starts with allowed AU prefixes
  # allowed list -> regex like ^(australia-southeast1|australia-southeast2)(-|$)
  local allowed_regex
  allowed_regex="^($(printf '%s' "$ALLOWED_AU_LOCATIONS" | sed 's/,/|/g'))(|-[a-z])?$"

  printf '%s' "$all" | jq -c --arg re "$allowed_regex" '
    [ .[]
      | select(.location != null)
      | select((.location | test($re)) | not)
      | {name, assetType, project: .project, location}
    ]'
}

fetch_recommenders_for_project() {
  local project_ref="$1" # expect numeric project number preferred
  local base="https://recommender.googleapis.com/v1/projects/${project_ref}/locations/global/recommenders"
  local resp
  resp=$(api_get "$base") || return 1
  printf '%s' "$resp" | jq -r '.recommenders[]?.name' 2>/dev/null
}

fetch_security_recommendations_for_project() {
  local project_number="$1"
  local recs="[]"
  local any=0

  # Enumerate recommenders (global). If this fails, try locations/- (best-effort)
  local names
  names=$(fetch_recommenders_for_project "$project_number" || true)
  if [[ -z "$names" ]]; then
    names=$(api_get "https://recommender.googleapis.com/v1/projects/${project_number}/locations/-/recommenders" | jq -r '.recommenders[]?.name' 2>/dev/null || true)
  fi

  if [[ -z "$names" ]]; then
    echo "$recs"; return 0
  fi

  while IFS= read -r rname; do
    [[ -z "$rname" ]] && continue
    local url="https://recommender.googleapis.com/v1/${rname}/recommendations?pageSize=1000"
    local pageToken=""
    while :; do
      local full_url="$url"
      [[ -n "$pageToken" ]] && full_url="${full_url}&pageToken=${pageToken}"
      local resp
      resp=$(api_get "$full_url") || { pageToken=""; break; }
      local items token
      items=$(printf '%s' "$resp" | jq -c '.recommendations // []')
      # Filter by SECURITY and ACTIVE client-side
      local filtered
      filtered=$(printf '%s' "$items" | jq -c '[.[] | select((.primaryImpact.category // .primaryImpact?.category) == "SECURITY") | select(.stateInfo.state=="ACTIVE" or .stateInfo?.state=="ACTIVE")]')
      recs=$(jq -c --argjson a "$recs" --argjson b "$filtered" '$a + $b' <<<"null")
      token=$(printf '%s' "$resp" | jq -r '.nextPageToken // empty')
      if [[ -z "$token" ]]; then break; fi
      pageToken="$token"
    done
  done <<< "$names"

  # Project number included for joining later
  printf '%s' "$recs" | jq -c '[.[] | {name, description:(.description // .content?.overview // ""), recommenderSubtype, priority:(.priority // .primaryImpact?.category // ""), state:(.stateInfo.state // .stateInfo?.state // ""), lastRefreshTime:(.lastRefreshTime // .updateTime // ""), projectNumber:"'"$project_number"'"}]'
}

################################################################################
# HTML rendering
################################################################################
render_html() {
  local org_id="$1" projects_json="$2" assets_json="$3" recs_json="$4" out_file="$5"

  local date_iso
  date_iso=$(date -u +'%Y-%m-%d %H:%M:%SZ')

  local proj_count
  proj_count=$(printf '%s' "$projects_json" | jq 'length')

  local assets_count
  assets_count=$(printf '%s' "$assets_json" | jq 'length')

  local recs_count
  recs_count=$(printf '%s' "$recs_json" | jq 'length')

  # Build simple tables as HTML fragments
  local projects_rows assets_rows recs_rows assets_summary_rows recs_summary_rows

  projects_rows=$(printf '%s' "$projects_json" | jq -r '
    [.[0:50][] | {projectId, projectNumber, displayName}] // [] | .[] |
    "<tr><td>" + (.projectId // "") + "</td><td>" + (.projectNumber // "") + "</td><td>" + (.displayName // "") + "</td></tr>"')

  assets_summary_rows=$(printf '%s' "$assets_json" | jq -r '
    group_by(.assetType) | map({assetType: .[0].assetType, count: length}) | sort_by(-.count) | .[] |
    "<tr><td>" + .assetType + "</td><td style=\"text-align:right\">" + ("" + (.count|tostring)) + "</td></tr>"')

  assets_rows=$(printf '%s' "$assets_json" | jq -r '
    .[0:100][] | "<tr><td>" + (.assetType // "") + "</td><td>" + (.project // "") + "</td><td>" + (.location // "") + "</td><td><code>" + (.name // "") + "</code></td></tr>"')

  recs_summary_rows=$(printf '%s' "$recs_json" | jq -r '
    group_by(.recommenderSubtype) | map({subtype: .[0].recommenderSubtype, count: length}) | sort_by(-.count) | .[] |
    "<tr><td>" + (.subtype // "") + "</td><td style=\"text-align:right\">" + ("" + (.count|tostring)) + "</td></tr>"')

  recs_rows=$(printf '%s' "$recs_json" | jq -r '
    .[0:100][] | "<tr><td>" + (.projectNumber // "") + "</td><td>" + (.recommenderSubtype // "") + "</td><td>" + (.priority // "") + "</td><td>" + (.state // "") + "</td><td>" + (.lastRefreshTime // "") + "</td><td>" + (.description // "") + "</td></tr>"')

  cat > "$out_file" <<HTML
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>GCP Org Security Report - Org $org_id</title>
  <style>
    body { font-family: Arial, Helvetica, sans-serif; color: #111; margin: 16px; }
    h1 { font-size: 20px; }
    h2 { font-size: 16px; margin-top: 24px; }
    .kpi { display: inline-block; margin-right: 24px; padding: 8px 12px; background:#f5f5f7; border-radius:8px; }
    table { border-collapse: collapse; width: 100%; margin-top: 8px; }
    th, td { border: 1px solid #ddd; padding: 6px 8px; vertical-align: top; }
    th { background: #f0f0f0; text-align: left; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
    .small { color: #555; font-size: 12px; }
  </style>
</head>
<body>
  <h1>GCP Organization Report - Org $org_id</h1>
  <div class="small">Generated: $date_iso (UTC)</div>
  <div style="margin-top:12px;">
    <span class="kpi"><strong>Active projects</strong>: $proj_count</span>
    <span class="kpi"><strong>Resources outside AU</strong>: $assets_count</span>
    <span class="kpi"><strong>Security recommendations</strong>: $recs_count</span>
  </div>

  <h2>Projects (sample)</h2>
  <table>
    <thead><tr><th>Project ID</th><th>Project Number</th><th>Display Name</th></tr></thead>
    <tbody>
      ${projects_rows}
    </tbody>
  </table>
  <div class="small">Showing up to 50 of $(printf '%s' "$projects_json" | jq 'length') projects.</div>

  <h2>Resources outside Australia</h2>
  <div class="small">Allowed AU locations: $(printf '%s' "$ALLOWED_AU_LOCATIONS")</div>
  <table>
    <thead><tr><th>Asset Type</th><th style="text-align:right">Count</th></tr></thead>
    <tbody>
      ${assets_summary_rows}
    </tbody>
  </table>
  <h3>Sample resources</h3>
  <table>
    <thead><tr><th>Asset Type</th><th>Project</th><th>Location</th><th>Name</th></tr></thead>
    <tbody>
      ${assets_rows}
    </tbody>
  </table>
  <div class="small">Showing up to 100 of $(printf '%s' "$assets_json" | jq 'length') resources.</div>

  <h2>Active Assist Security Recommendations</h2>
  <table>
    <thead><tr><th>Subtype</th><th style="text-align:right">Count</th></tr></thead>
    <tbody>
      ${recs_summary_rows}
    </tbody>
  </table>
  <h3>Sample recommendations</h3>
  <table>
    <thead><tr><th>Project #</th><th>Subtype</th><th>Priority</th><th>State</th><th>Last Refresh</th><th>Description</th></tr></thead>
    <tbody>
      ${recs_rows}
    </tbody>
  </table>
  <div class="small">Showing up to 100 of $(printf '%s' "$recs_json" | jq 'length') recommendations.</div>

  <h2>Notes</h2>
  <ul>
    <li>Data pulled via REST APIs: Resource Manager v3, Cloud Asset Inventory, Recommender.</li>
    <li>Security recommendations filtered by primaryImpact.category = SECURITY and state = ACTIVE.</li>
    <li>Resources are considered outside AU if their location does not match: $(printf '%s' "$ALLOWED_AU_LOCATIONS").</li>
  </ul>
</body>
</html>
HTML
}

################################################################################
# Main
################################################################################
main() {
  parse_args "$@"
  mkwork
  require_tools
  ensure_access_token

  log "Fetching active projects in organization ${ORG_ID}"
  local projects_json
  projects_json=$(fetch_projects)
  local project_count
  project_count=$(printf '%s' "$projects_json" | jq 'length')
  log "Found ${project_count} active projects"

  log "Searching for resources outside AU via Cloud Asset Inventory"
  local assets_json
  if [[ "$SKIP_CAI" -eq 1 ]]; then
    log "Skipping CAI section due to --skip-cai"
    assets_json='[]'
  else
    assets_json=$(fetch_cai_outside_au)
    log "Found $(printf '%s' "$assets_json" | jq 'length') resources outside AU"
  fi

  log "Fetching Active Assist SECURITY recommendations per project"
  local recs_all="[]"
  # Iterate projects and collect recommendations
  if [[ "$SKIP_RECOMMENDER" -eq 1 ]]; then
    log "Skipping recommender section due to --skip-recommender"
  else
    local p_count=0
    if [[ "${PROJECTS_LIMIT:-0}" -gt 0 ]]; then
      log "Limiting recommender queries to first ${PROJECTS_LIMIT} projects"
      printf '%s' "$projects_json" | jq -r --argjson n "$PROJECTS_LIMIT" '.[0:$n][] | .projectNumber' | while read -r pnum; do
        [[ -z "$pnum" ]] && continue
        p_count=$((p_count+1)) || true
        log "Project ${pnum}: querying recommender list and recommendations"
        local recs
        recs=$(fetch_security_recommendations_for_project "$pnum") || recs='[]'
        printf '%s\n' "$recs"
      done > "$WORK_DIR/recs_stream.jsonl"
    else
      printf '%s' "$projects_json" | jq -r '.[] | .projectNumber' | while read -r pnum; do
        [[ -z "$pnum" ]] && continue
        p_count=$((p_count+1)) || true
        log "Project ${pnum}: querying recommender list and recommendations"
        local recs
        recs=$(fetch_security_recommendations_for_project "$pnum") || recs='[]'
        printf '%s\n' "$recs"
      done > "$WORK_DIR/recs_stream.jsonl"
    fi
  fi

  if [[ -s "$WORK_DIR/recs_stream.jsonl" ]]; then
    recs_all=$(jq -s 'flatten' "$WORK_DIR/recs_stream.jsonl")
  fi
  log "Collected $(printf '%s' "$recs_all" | jq 'length') security recommendations"

  log "Rendering HTML report to ${OUTPUT_FILE}"
  render_html "$ORG_ID" "$projects_json" "$assets_json" "$recs_all" "$OUTPUT_FILE"
  log "Report written: ${OUTPUT_FILE}"
  printf '%s\n' "$OUTPUT_FILE"
}

main "$@"
