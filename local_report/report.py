#!/usr/bin/env python3
"""
Generate a GCP org HTML report using an OAuth2 access token (no gcloud, no SA).
Outputs report_YYYYMMDDTHHMMSSZ.html in the current directory by default.

Data included:
- Total number of ACTIVE projects in the organization (via Cloud Resource Manager)
- Resources outside AU (via Cloud Asset Inventory at org scope)
- Active Assist recommendations with SECURITY impact at org scope (best effort)

Notes:
- For Cloud Asset Inventory org-scope calls you often must pass a quota/billing
  project with the API enabled using the --quota-project option. That adds the
  x-goog-user-project header to requests.
- Your token must have at least the scopes/permissions for the data you are
  retrieving (e.g., roles/browser, roles/cloudasset.viewer, roles/recommender.viewer).

Example:
  python report.py \
    --org-id 123456789012 \
    --token "$ACCESS_TOKEN" \
    --quota-project YOUR_PROJECT_ID
"""
import argparse
import datetime
import html
import json
from typing import Any, Dict, List, Optional

import requests


def iso_now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")


def ts_for_fname() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def make_session(token: str, quota_project: Optional[str], timeout: int) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    if quota_project:
        s.headers.update({"x-goog-user-project": quota_project})
    # default timeout
    orig_request = s.request

    def _req(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return orig_request(method, url, **kwargs)

    s.request = _req  # type: ignore
    return s


def get_projects_count(session: requests.Session, org_id: str) -> int:
    # Try CRM v3 projects:search first
    url = "https://cloudresourcemanager.googleapis.com/v3/projects:search"
    query = f"parent=organizations/{org_id} state:ACTIVE"
    payload = {"query": query, "pageSize": 1000}
    total = 0
    page_token: Optional[str] = None
    while True:
        body = dict(payload)
        if page_token:
            body["pageToken"] = page_token
        r = session.post(url, json=body)
        if r.status_code // 100 != 2:
            # Fallback to v1
            break
        data = r.json()
        total += len(data.get("projects", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return total

    # v1 fallback
    base = "https://cloudresourcemanager.googleapis.com/v1/projects"
    params: Dict[str, Any] = {
        "pageSize": 500,
        "filter": f"parent.type:organization parent.id:{org_id} lifecycleState:ACTIVE",
    }
    page_token = None
    count = 0
    while True:
        if page_token:
            params["pageToken"] = page_token
        r = session.get(base, params=params)
        if r.status_code // 100 != 2:
            raise RuntimeError(f"CRM list projects error {r.status_code}: {r.text}")
        data = r.json()
        count += len(data.get("projects", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return count


def search_cai_outside_au(session: requests.Session, org_id: str) -> Dict[str, List[Dict[str, str]]]:
    """Return mapping projectId -> list of resources outside AU.
    """
    scope = f"organizations/{org_id}"
    url = f"https://cloudasset.googleapis.com/v1/{scope}:searchAllResources"
    # Hardcoded AU filter (adjust as needed)
    query = "location:* AND NOT (location:australia-southeast1* OR location:australia-southeast2*)"
    body: Dict[str, Any] = {"pageSize": 1000, "query": query}

    results: Dict[str, List[Dict[str, str]]] = {}
    next_token: Optional[str] = None
    while True:
        b = dict(body)
        if next_token:
            b["pageToken"] = next_token
        r = session.post(url, json=b)
        if r.status_code // 100 != 2:
            # Best effort: if CAI fails, return empty
            return {}
        data = r.json()
        for res in data.get("results", []):
            proj_ref = res.get("project")  # like "projects/PROJECT_ID"
            project_id = proj_ref.split("/")[1] if isinstance(proj_ref, str) and "/" in proj_ref else proj_ref
            item = {
                "name": res.get("name", ""),
                "type": res.get("assetType", ""),
                "location": res.get("location", ""),
            }
            results.setdefault(project_id or "unknown", []).append(item)
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return results


def list_org_recommenders(session: requests.Session, org_id: str) -> List[str]:
    url = f"https://recommender.googleapis.com/v1/organizations/{org_id}/locations/-/recommenders"
    r = session.get(url)
    if r.status_code // 100 != 2:
        return []
    data = r.json()
    names = [x.get("name") for x in data.get("recommenders", []) if x.get("name")]
    return names


def list_org_security_recommendations(session: requests.Session, org_id: str) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    rnames = list_org_recommenders(session, org_id)
    for rname in rnames:
        page_token: Optional[str] = None
        while True:
            url = f"https://recommender.googleapis.com/v1/{rname}/recommendations?pageSize=1000&filter=stateInfo.state=\"ACTIVE\""
            if page_token:
                url += f"&pageToken={page_token}"
            r = session.get(url)
            if r.status_code // 100 != 2:
                break
            data = r.json()
            for it in data.get("recommendations", []):
                primary = (it.get("primaryImpact") or {}).get("category")
                if primary != "SECURITY":
                    continue
                recs.append({
                    "name": it.get("name", ""),
                    "recommenderSubtype": it.get("recommenderSubtype", ""),
                    "priority": it.get("priority") or primary or "",
                    "state": ((it.get("stateInfo") or {}).get("state")) or "",
                    "lastRefreshTime": it.get("lastRefreshTime") or it.get("updateTime") or "",
                    "description": it.get("description") or ((it.get("content") or {}).get("overview")) or "",
                })
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return recs


def render_html(org_id: str,
                project_count: int,
                resources_map: Dict[str, List[Dict[str, str]]],
                security_recs: List[Dict[str, Any]]) -> str:
    date_iso = iso_now()

    # Build resources table rows grouped by project
    resource_rows: List[str] = []
    for pid, res_list in sorted(resources_map.items(), key=lambda kv: kv[0] or ""):
        for r in res_list:
            resource_rows.append(
                f"<tr><td>{html.escape(pid or '')}</td><td>{html.escape(r.get('name',''))}</td><td>{html.escape(r.get('type',''))}</td><td>{html.escape(r.get('location',''))}</td></tr>"
            )
    if not resource_rows:
        resource_rows.append("<tr><td colspan='4'>No resources outside AU</td></tr>")

    # Build security recommendations rows
    rec_rows: List[str] = []
    for r in security_recs[:500]:
        rec_rows.append(
            f"<tr><td>{html.escape(r.get('recommenderSubtype',''))}</td><td>{html.escape(r.get('priority',''))}</td><td>{html.escape(r.get('state',''))}</td><td>{html.escape(r.get('lastRefreshTime',''))}</td><td>{html.escape(r.get('description',''))}</td></tr>"
        )
    if not rec_rows:
        rec_rows.append("<tr><td colspan='5'>No security recommendations found (or API not enabled)</td></tr>")

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>GCP Org Security Report - Org {org_id}</title>
  <style>
    body {{ font-family: Arial, Helvetica, sans-serif; color: #111; margin: 16px; }}
    h1 {{ font-size: 20px; }}
    h2 {{ font-size: 16px; margin-top: 24px; }}
    .kpi {{ display: inline-block; margin-right: 24px; padding: 8px 12px; background:#f5f5f7; border-radius:8px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 8px; vertical-align: top; }}
    th {{ background: #f0f0f0; text-align: left; }}
  </style>
</head>
<body>
  <h1>GCP Organization Report - Org {org_id}</h1>
  <div>Generated: {date_iso} (UTC)</div>
  <div style=\"margin-top:12px;\">
    <span class=\"kpi\"><strong>Total projects</strong>: {project_count}</span>
  </div>

  <h2>Resources outside Australia</h2>
  <table>
    <thead><tr><th>Project ID</th><th>Resource Name</th><th>Resource Type</th><th>Location</th></tr></thead>
    <tbody>
      {''.join(resource_rows)}
    </tbody>
  </table>

  <h2>Active Assist Security Recommendations (Org)</h2>
  <table>
    <thead><tr><th>Subtype</th><th>Priority</th><th>State</th><th>Last Refresh</th><th>Description</th></tr></thead>
    <tbody>
      {''.join(rec_rows)}
    </tbody>
  </table>
</body>
</html>"""
    return html_doc


def main():
    ap = argparse.ArgumentParser(description="Generate GCP org security HTML report (token-based)")
    ap.add_argument("--org-id", required=True, help="Organization numeric ID (e.g., 123456789012)")
    ap.add_argument("--token", required=True, help="OAuth2 access token with cloud-platform scope")
    ap.add_argument("--quota-project", help="Quota/billing project for x-goog-user-project header (enable APIs there)")
    ap.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds")
    ap.add_argument("--output-dir", default=".", help="Directory to write HTML report")
    args = ap.parse_args()

    sess = make_session(args.token, args.quota_project, args.timeout)

    # 1) Projects count
    proj_count = get_projects_count(sess, args.org_id)

    # 2) Resources outside AU (org)
    resources_map = search_cai_outside_au(sess, args.org_id)

    # 3) Org-level security recommendations (best effort)
    security_recs = list_org_security_recommendations(sess, args.org_id)

    html_doc = render_html(args.org_id, proj_count, resources_map, security_recs)

    out_path = f"report_{ts_for_fname()}.html"
    if args.output_dir:
        out_path = (args.output_dir.rstrip("/")) + "/" + out_path

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(out_path)


if __name__ == "__main__":
    main()
