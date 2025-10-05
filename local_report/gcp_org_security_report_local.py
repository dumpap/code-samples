#!/usr/bin/env python3
"""
Standalone local Python script to generate a GCP Org Security HTML report.
- No Cloud Functions, no GCS upload. Writes report_YYYYMMDDTHHMMSSZ.html locally.
- Uses a user-provided OAuth2 access token (no service account, no gcloud).
- Uses Google REST APIs via requests:
  * Cloud Resource Manager v3 (fallback to v1) to list projects, or CAI-only path if requested
  * Cloud Asset Inventory (CAI) to find resources outside AU
  * Recommender (optional) for Active Assist SECURITY recommendations

Requirements:
  pip install requests
  (jq not required; script is pure Python)

Example:
  python gcp_org_security_report_local.py \
    --org-id 123456789012 \
    --token "$ACCESS_TOKEN" \
    --use-cai-projects \
    --allowed-au "australia-southeast1,australia-southeast2" \
    --enable-recommender
"""
import argparse
import datetime
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

import requests


def iso_utc() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_for_filename() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def make_session(token: str, quota_project: Optional[str] = None, timeout: int = 60) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    if quota_project:
        s.headers.update({"x-goog-user-project": quota_project})
    s.request = _with_timeout(s.request, timeout)
    return s


def _with_timeout(request_func, timeout: int):
    def wrapper(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return request_func(method, url, **kwargs)
    return wrapper


def list_projects(session: requests.Session, org_id: Optional[str]) -> List[Dict[str, str]]:
    """List active projects. Prefer CRM v3 projects:search; fallback to v1 projects.list."""
    projects: List[Dict[str, str]] = []
    # v3 projects:search
    if org_id:
        url = "https://cloudresourcemanager.googleapis.com/v3/projects:search"
        query = f"parent=organizations/{org_id} state:ACTIVE"
        payload = {"query": query, "pageSize": 1000}
        page_token = None
        while True:
            if page_token:
                payload["pageToken"] = page_token
            r = session.post(url, json=payload)
            if r.status_code // 100 != 2:
                # fallback to v1
                break
            data = r.json()
            for p in data.get("projects", []):
                projects.append({
                    "projectId": p.get("projectId"),
                    "displayName": p.get("displayName") or p.get("projectId"),
                })
            page_token = data.get("nextPageToken")
            if not page_token:
                return projects
    # v1 fallback
    base = "https://cloudresourcemanager.googleapis.com/v1/projects"
    filter_str = None
    if org_id:
        filter_str = f"parent.type:organization parent.id:{org_id} lifecycleState:ACTIVE"
    params = {"pageSize": 500}
    if filter_str:
        params["filter"] = filter_str
    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        r = session.get(base, params=params)
        if r.status_code // 100 != 2:
            raise RuntimeError(f"CRM list projects error {r.status_code}: {r.text}")
        data = r.json()
        for p in data.get("projects", []):
            projects.append({
                "projectId": p.get("projectId"),
                "displayName": p.get("name") or p.get("projectId"),
            })
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return projects


def search_cai_resources(session: requests.Session, scope: str, query: Optional[str] = None,
                          asset_types: Optional[List[str]] = None, page_size: int = 500) -> List[Dict[str, Any]]:
    url = f"https://cloudasset.googleapis.com/v1/{scope}:searchAllResources"
    body: Dict[str, Any] = {"pageSize": page_size}
    if query:
        body["query"] = query
    if asset_types:
        body["assetTypes"] = asset_types

    results: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    while True:
        if next_token:
            body["pageToken"] = next_token
        r = session.post(url, json=body)
        if r.status_code // 100 != 2:
            raise RuntimeError(f"CAI search error {r.status_code}: {r.text}")
        data = r.json()
        results.extend(data.get("results", []))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return results


def projects_from_cai(session: requests.Session, org_id: str) -> List[Dict[str, str]]:
    scope = f"organizations/{org_id}"
    results = search_cai_resources(
        session,
        scope,
        query="state:ACTIVE",
        asset_types=["cloudresourcemanager.googleapis.com/Project"],
        page_size=1000,
    )
    projects: List[Dict[str, str]] = []
    for r in results:
        proj_ref = r.get("project")  # projects/PROJECT_ID
        project_id = proj_ref.split("/")[1] if isinstance(proj_ref, str) and "/" in proj_ref else proj_ref
        display_name = r.get("displayName") or r.get("name") or project_id
        projects.append({"projectId": project_id, "displayName": display_name})
    return projects


def non_au_query(allowed_csv: str) -> str:
    allowed = [x.strip() for x in allowed_csv.split(",") if x.strip()]
    if not allowed:
        return "location:*"
    disj = " OR ".join([f"location:{loc}*" for loc in allowed])
    return f"location:* AND NOT ({disj})"


def resources_outside_au(session: requests.Session, org_id: str, allowed_csv: str) -> List[Dict[str, Any]]:
    scope = f"organizations/{org_id}"
    query = non_au_query(allowed_csv)
    results = search_cai_resources(session, scope, query=query, page_size=1000)
    trimmed: List[Dict[str, Any]] = []
    for r in results:
        trimmed.append({
            "name": r.get("name"),
            "assetType": r.get("assetType"),
            "project": r.get("project"),
            "location": r.get("location"),
        })
    return trimmed


def list_recommenders(session: requests.Session, project_id: str) -> List[str]:
    url = f"https://recommender.googleapis.com/v1/projects/{project_id}/locations/-/recommenders"
    r = session.get(url)
    if r.status_code // 100 != 2:
        return []
    data = r.json()
    return [x.get("name") for x in data.get("recommenders", []) if x.get("name")]


def list_security_recommendations(session: requests.Session, project_id: str) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    names = list_recommenders(session, project_id)
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
                    "name": it.get("name"),
                    "projectId": project_id,
                    "recommenderSubtype": it.get("recommenderSubtype"),
                    "priority": it.get("priority") or primary or "",
                    "state": ((it.get("stateInfo") or {}).get("state")) or "",
                    "lastRefreshTime": it.get("lastRefreshTime") or it.get("updateTime") or "",
                    "description": it.get("description") or ((it.get("content") or {}).get("overview")) or "",
                })
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return recs


def render_html(org_id: str, projects: List[Dict[str, str]], assets: List[Dict[str, Any]], recs: List[Dict[str, Any]], allowed_csv: str) -> str:
    date_iso = iso_utc()
    proj_count = len(projects)
    assets_count = len(assets)
    recs_count = len(recs)

    asset_type_counts = Counter([a.get("assetType", "") for a in assets])
    rec_subtype_counts = Counter([r.get("recommenderSubtype", "") for r in recs])

    assets_summary_rows = "\n".join(
        f"<tr><td>{k}</td><td style=\"text-align:right\">{v}</td></tr>"
        for k, v in sorted(asset_type_counts.items(), key=lambda kv: kv[1], reverse=True)
    )
    recs_summary_rows = "\n".join(
        f"<tr><td>{k}</td><td style=\"text-align:right\">{v}</td></tr>"
        for k, v in sorted(rec_subtype_counts.items(), key=lambda kv: kv[1], reverse=True)
    )

    def _e(s: Optional[str]) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    projects_rows = "\n".join(
        f"<tr><td>{_e(p['projectId'])}</td><td>{_e(p['displayName'])}</td></tr>"
        for p in projects[:200]
    )
    assets_rows = "\n".join(
        f"<tr><td>{_e(a.get('assetType',''))}</td><td>{_e(a.get('project',''))}</td><td>{_e(a.get('location',''))}</td><td><code>{_e(a.get('name',''))}</code></td></tr>"
        for a in assets[:200]
    )
    recs_rows = "\n".join(
        f"<tr><td>{_e(r.get('projectId',''))}</td><td>{_e(r.get('recommenderSubtype',''))}</td><td>{_e(r.get('priority',''))}</td><td>{_e(r.get('state',''))}</td><td>{_e(r.get('lastRefreshTime',''))}</td><td>{_e(r.get('description',''))}</td></tr>"
        for r in recs[:200]
    )

    html = f"""<!DOCTYPE html>
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
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    .small {{ color: #555; font-size: 12px; }}
  </style>
</head>
<body>
  <h1>GCP Organization Report - Org {org_id}</h1>
  <div class=\"small\">Generated: {date_iso} (UTC)</div>
  <div style=\"margin-top:12px;\">
    <span class=\"kpi\"><strong>Active projects</strong>: {proj_count}</span>
    <span class=\"kpi\"><strong>Resources outside AU</strong>: {assets_count}</span>
    <span class=\"kpi\"><strong>Security recommendations</strong>: {recs_count}</span>
  </div>

  <h2>Projects</h2>
  <table>
    <thead><tr><th>Project ID</th><th>Display Name</th></tr></thead>
    <tbody>
      {projects_rows}
    </tbody>
  </table>

  <h2>Resources outside Australia</h2>
  <div class=\"small\">Allowed AU locations: {allowed_csv}</div>
  <table>
    <thead><tr><th>Asset Type</th><th style=\"text-align:right\">Count</th></tr></thead>
    <tbody>
      {assets_summary_rows}
    </tbody>
  </table>
  <table>
    <thead><tr><th>Asset Type</th><th>Project</th><th>Location</th><th>Name</th></tr></thead>
    <tbody>
      {assets_rows}
    </tbody>
  </table>

  <h2>Active Assist Security Recommendations</h2>
  <table>
    <thead><tr><th>Subtype</th><th style=\"text-align:right\">Count</th></tr></thead>
    <tbody>
      {recs_summary_rows}
    </tbody>
  </table>
  <table>
    <thead><tr><th>Project</th><th>Subtype</th><th>Priority</th><th>State</th><th>Last Refresh</th><th>Description</th></tr></thead>
    <tbody>
      {recs_rows}
    </tbody>
  </table>

</body>
</html>"""
    return html


def main():
    ap = argparse.ArgumentParser(description="Generate GCP Org Security HTML report (local, token-based)")
    ap.add_argument("--org-id", required=True, help="Organization numeric ID (e.g., 123456789012)")
    ap.add_argument("--token", required=True, help="OAuth2 access token with cloud-platform scope")
    ap.add_argument("--quota-project", help="Quota/billing project for APIs (x-goog-user-project header)")
    ap.add_argument("--allowed-au", default="australia-southeast1,australia-southeast2", help="CSV list of allowed AU location prefixes")
    ap.add_argument("--use-cai-projects", action="store_true", help="Fetch projects via Cloud Asset Inventory instead of Resource Manager")
    ap.add_argument("--enable-recommender", action="store_true", help="Fetch Active Assist SECURITY recommendations")
    ap.add_argument("--output-dir", default=".", help="Directory to write the HTML report")
    ap.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds")
    args = ap.parse_args()

    session = make_session(args.token, quota_project=args.quota_project, timeout=args.timeout)

    if args.use_cai_projects:
        projects = projects_from_cai(session, args.org_id)
    else:
        projects = list_projects(session, args.org_id)

    try:
        assets = resources_outside_au(session, args.org_id, args.allowed_au)
    except Exception:
        assets = []

    recs: List[Dict[str, Any]] = []
    if args.enable_recommender:
        for p in projects:
            pid = p["projectId"]
            try:
                recs.extend(list_security_recommendations(session, pid))
            except Exception:
                pass

    html = render_html(args.org_id, projects, assets, recs, args.allowed_au)

    os.makedirs(args.output_dir, exist_ok=True)
    fn = os.path.join(args.output_dir, f"report_{ts_for_filename()}.html")
    with open(fn, "w", encoding="utf-8") as f:
        f.write(html)
    print(fn)


if __name__ == "__main__":
    main()
