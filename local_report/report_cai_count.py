#!/usr/bin/env python3
"""
Generate a GCP org HTML report using an OAuth2 access token (no gcloud, no SA).
This version uses Cloud Asset Inventory (CAI) ONLY to count projects.
Outputs report_YYYYMMDDTHHMMSSZ.html in the current directory by default.

Data included:
- Total number of ACTIVE projects in the organization (via CAI searchAllResources)
- Resources outside AU (via CAI at org scope)
- Active Assist recommendations with SECURITY impact at org scope (best effort)

Notes:
- For CAI org-scope calls you typically must pass a quota/billing project with
  cloudasset API enabled using --quota-project (adds x-goog-user-project header).
- Your token must have org-level visibility and roles/cloudasset.viewer,
  roles/recommender.viewer (for recommendations), etc.

Example:
  python report_cai_count.py \
    --org-id 123456789012 \
    --token "$ACCESS_TOKEN" \
    --quota-project YOUR_PROJECT_ID
"""
import argparse
import datetime
import html
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
    orig_request = s.request

    def _req(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return orig_request(method, url, **kwargs)

    s.request = _req  # type: ignore
    return s


def cai_search(session: requests.Session, scope: str, query: Optional[str] = None,
               asset_types: Optional[List[str]] = None, page_size: int = 1000) -> List[Dict[str, Any]]:
    url = f"https://cloudasset.googleapis.com/v1/{scope}:searchAllResources"
    body: Dict[str, Any] = {"pageSize": page_size}
    if query:
        body["query"] = query
    if asset_types:
        body["assetTypes"] = asset_types
    results: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    while True:
        b = dict(body)
        if next_token:
            b["pageToken"] = next_token
        r = session.post(url, json=b)
        if r.status_code // 100 != 2:
            raise RuntimeError(f"CAI search error {r.status_code}: {r.text}")
        data = r.json()
        results.extend(data.get("results", []))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return results


def get_projects_count_cai(session: requests.Session, org_id: str) -> int:
    scope = f"organizations/{org_id}"
    results = cai_search(
        session,
        scope,
        query="state:ACTIVE",
        asset_types=["cloudresourcemanager.googleapis.com/Project"],
        page_size=1000,
    )
    return len(results)


def search_cai_outside_au(session: requests.Session, org_id: str) -> Dict[str, List[Dict[str, str]]]:
    scope = f"organizations/{org_id}"
    url = f"https://cloudasset.googleapis.com/v1/{scope}:searchAllResources"
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
            return {}
        data = r.json()
        for res in data.get("results", []):
            proj_ref = res.get("project")  # projects/PROJECT_ID
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
    return [x.get("name") for x in data.get("recommenders", []) if x.get("name")]


def list_org_security_recommendations(session: requests.Session, org_id: str) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    names = list_org_recommenders(session, org_id)
    for rname in names:
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

    resource_rows: List[str] = []
    for pid, res_list in sorted(resources_map.items(), key=lambda kv: kv[0] or ""):
        for r in res_list:
            resource_rows.append(
                f"<tr><td>{html.escape(pid or '')}</td><td>{html.escape(r.get('name',''))}</td><td>{html.escape(r.get('type',''))}</td><td>{html.escape(r.get('location',''))}</td></tr>"
            )
    if not resource_rows:
        resource_rows.append("<tr><td colspan='4'>No resources outside AU</td></tr>")

    rec_rows: List[str] = []
    for r in security_recs[:500]:
        rec_rows.append(
            f"<tr><td>{html.escape(r.get('recommenderSubtype',''))}</td><td>{html.escape(r.get('priority',''))}</td><td>{html.escape(r.get('state',''))}</td><td>{html.escape(r.get('lastRefreshTime',''))}</td><td>{html.escape(r.get('description',''))}</td></tr>"
        )
    if not rec_rows:
        rec_rows.append("<tr><td colspan='5'>No security recommendations found (or API not enabled)</td></tr>")

    return f"""<!DOCTYPE html>
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
  <div style=\"margin-top:12px;\"><span class=\"kpi\"><strong>Total projects (CAI)</strong>: {project_count}</span></div>

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


def main():
    ap = argparse.ArgumentParser(description="Generate GCP org security HTML report (CAI-only project count)")
    ap.add_argument("--org-id", required=True, help="Organization numeric ID (e.g., 123456789012)")
    ap.add_argument("--token", required=True, help="OAuth2 access token with cloud-platform scope")
    ap.add_argument("--quota-project", help="Quota/billing project for x-goog-user-project header (enable APIs there)")
    ap.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds")
    ap.add_argument("--output-dir", default=".", help="Directory to write HTML report")
    args = ap.parse_args()

    sess = make_session(args.token, args.quota_project, args.timeout)

    proj_count = get_projects_count_cai(sess, args.org_id)
    resources_map = search_cai_outside_au(sess, args.org_id)
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
