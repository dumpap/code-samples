#!/usr/bin/env bash
set -euo pipefail

# Simple GCP Org HTML report using REST APIs and a provided OAuth2 token
# - Projects count (Resource Manager v1)
# - Resources outside AU (Cloud Asset Inventory at org scope)
# - Output: HTML file
# No gcloud; requires: curl, jq

SCRIPT_NAME="$(basename "$0")"
ORG_ID=""
ACCESS_TOKEN=""
OUTPUT_FILE="./report.html"
ALLOWED_AU_LOCATIONS="australia-southeast1,australia-southeast2"
QUOTA_PROJECT=""  # optional: sets x-goog-user-project header for CAI
HTTP_TIMEOUT=60

usage() {
  cat <<EOF
$SCRIPT_NAME - Generate simple GCP org HTML report

Required:
  --org-id ORG_ID           Organization numeric ID (e.g., 123456789012)
  --token ACCESS_TOKEN      OAuth2 access token (scope: cloud-platform)

Options:
  --output-file PATH        Output HTML file (default: ./report.html)
  --allowed-au CSV          Allowed AU locations (default: australia-southeast1,australia-southeast2)
  --quota-project PROJECT   Quota/billing project for CAI (x-goog-user-project)
  --timeout SECONDS         HTTP timeout (default: 60)
  -h, --help                Show help

Notes:
  - Requires curl and jq.
  - If CAI 404/403 occurs, enable cloudasset API on --quota-project and pass it.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --org-id) ORG_ID="$2"; shift 2;;
    --token) ACCESS_TOKEN="$2"; shift 2;;
    --output-file) OUTPUT_FILE="$2"; shift 2;;
    --allowed-au) ALLOWED_AU_LOCATIONS="$2"; shift 2;;
    --quota-project) QUOTA_PROJECT="$2"; shift 2;;
    --timeout) HTTP_TIMEOUT="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1;;
  esac
done

[[ -n "$ORG_ID" ]] || { echo "--org-id is required" >&2; exit 1; }
[[ -n "$ACCESS_TOKEN" ]] || { echo "--token is required" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }

auth_header=( -H "Authorization: Bearer ${ACCESS_TOKEN}" )
[[ -n "$QUOTA_PROJECT" ]] && cai_quota_header=( -H "x-goog-user-project: ${QUOTA_PROJECT}" ) || cai_quota_header=()

# 1) Projects count via Resource Manager v1
projects_count=0
pageToken=""
crm_base="https://cloudresourcemanager.googleapis.com/v1/projects"
crm_filter="parent.type:organization parent.id:${ORG_ID} lifecycleState:ACTIVE"
while :; do
  # Use -G with --data-urlencode for safe query encoding
  if [[ -n "$pageToken" ]]; then
    resp=$(curl -sS -G "${crm_base}" "${auth_header[@]}" \
      --max-time "$HTTP_TIMEOUT" \
      --data-urlencode "pageSize=500" \
      --data-urlencode "filter=${crm_filter}" \
      --data-urlencode "pageToken=${pageToken}")
  else
    resp=$(curl -sS -G "${crm_base}" "${auth_header[@]}" \
      --max-time "$HTTP_TIMEOUT" \
      --data-urlencode "pageSize=500" \
      --data-urlencode "filter=${crm_filter}")
  fi
  projects_count=$(( projects_count + $(printf '%s' "$resp" | jq '.projects | length') ))
  pageToken=$(printf '%s' "$resp" | jq -r '.nextPageToken // empty')
  [[ -z "$pageToken" ]] && break
done

# 2) Resources outside AU via CAI at org scope
# Hardcoded filter: NOT in allowed AU locations
allowed_regex_csv="$ALLOWED_AU_LOCATIONS"
# Build CAI query like: location:* AND NOT (location:australia-southeast1* OR location:australia-southeast2*)
IFS=',' read -r -a allowed_arr <<< "$ALLOWED_AU_LOCATIONS"
joiner=""
query_part=""
for loc in "${allowed_arr[@]}"; do
  loc_trimmed="${loc// /}"
  [[ -z "$loc_trimmed" ]] && continue
  if [[ -z "$joiner" ]]; then
    query_part+="location:${loc_trimmed}*"
    joiner=" OR "
  else
    query_part+="${joiner}location:${loc_trimmed}*"
  fi
done
cai_query="location:* AND NOT (${query_part})"

cai_url="https://cloudasset.googleapis.com/v1/organizations/${ORG_ID}:searchAllResources"
pageToken=""
resource_rows=""
while :; do
  if [[ -n "$pageToken" ]]; then
    body=$(jq -n --arg q "$cai_query" --arg pt "$pageToken" '{pageSize:500, query:$q, pageToken:$pt}')
  else
    body=$(jq -n --arg q "$cai_query" '{pageSize:500, query:$q}')
  fi
  resp=$(curl -sS -X POST "$cai_url" "${auth_header[@]}" "${cai_quota_header[@]}" \
    -H 'Content-Type: application/json' --max-time "$HTTP_TIMEOUT" -d "$body") || resp='{}'
  # Append table rows: Project ID and Resource Name
  # project comes like "projects/PROJECT_ID"
  rows=$(printf '%s' "$resp" | jq -r '.results[]? | [ .project, .name ] | @tsv' | awk -F"\t" '{gsub(/^projects\//, "", $1); printf("<tr><td>%s</td><td><code>%s</code></td></tr>\n", $1, $2)}')
  resource_rows+="$rows"
  pageToken=$(printf '%s' "$resp" | jq -r '.nextPageToken // empty')
  [[ -z "$pageToken" ]] && break
done

# 3) HTML output (minimal)
now_utc="$(date -u +'%Y-%m-%d %H:%M:%SZ')"
cat > "$OUTPUT_FILE" <<HTML
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>GCP Org Report - Org ${ORG_ID}</title>
  <style>
    body { font-family: Arial, Helvetica, sans-serif; color: #111; margin: 16px; }
    h1 { font-size: 20px; }
    h2 { font-size: 16px; margin-top: 20px; }
    .kpi { display:inline-block; margin-right:16px; padding:6px 10px; background:#f5f5f7; border-radius:6px; }
    table { border-collapse: collapse; width: 100%; margin-top: 8px; }
    th, td { border: 1px solid #ddd; padding: 6px 8px; vertical-align: top; }
    th { background: #f0f0f0; text-align: left; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
  </style>
</head>
<body>
  <h1>GCP Organization Report - Org ${ORG_ID}</h1>
  <div>Generated: ${now_utc} (UTC)</div>
  <div style="margin-top:10px;">
    <span class="kpi"><strong>Total projects</strong>: ${projects_count}</span>
  </div>

  <h2>Resources outside Australia</h2>
  <div>Allowed AU locations: ${ALLOWED_AU_LOCATIONS}</div>
  <table>
    <thead><tr><th>Project ID</th><th>Resource Name</th></tr></thead>
    <tbody>
${resource_rows}
    </tbody>
  </table>
</body>
</html>
HTML

printf '%s\n' "$OUTPUT_FILE"
