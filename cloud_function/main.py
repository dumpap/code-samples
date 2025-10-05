import os
import re
import json
import datetime
from typing import Dict, List, Any, Optional

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage


ORG_ID = os.environ.get("ORG_ID", "")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "")
ALLOWED_AU_LOCATIONS = os.environ.get("ALLOWED_AU_LOCATIONS", "australia-southeast1,australia-southeast2")
RECOMMENDER_ENABLED = os.environ.get("RECOMMENDER_ENABLED", "true").lower() == "true"
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "60"))


def _auth_session() -> AuthorizedSession:
    credentials, project_id = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    # Ensure quota/billing project is set for org-scope APIs like CAI
    if project_id:
        session.headers["x-goog-user-project"] = project_id
    return session


def _cai_search_all_resources(session: AuthorizedSession, scope: str, query: Optional[str] = None,
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
        resp = session.post(url, json=body, timeout=HTTP_TIMEOUT)
        if resp.status_code // 100 != 2:
            # Return what we have so caller can decide; raise on critical usage outside
            raise RuntimeError(f"CAI search error {resp.status_code}: {resp.text}")
        data = resp.json()
        results.extend(data.get("results", []))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return results


def _make_non_au_query(allowed_locations: List[str]) -> str:
    # Build CAI query to return resources with a location that is not any allowed AU prefix
    # Example: location:* AND NOT (location:australia-southeast1* OR location:australia-southeast2*)
    disj = " OR ".join([f"location:{loc}*" for loc in allowed_locations if loc])
    if not disj:
        return "location:*"
    return f"location:* AND NOT ({disj})"


def _projects_from_cai(session: AuthorizedSession, org_id: str) -> List[Dict[str, str]]:
    scope = f"organizations/{org_id}"
    # Query only projects
    projects = _cai_search_all_resources(
        session,
        scope,
        query="state:ACTIVE",
        asset_types=["cloudresourcemanager.googleapis.com/Project"],
        page_size=1000,
    )
    simplified: List[Dict[str, str]] = []
    for p in projects:
        # p["project"] is like "projects/PROJECT_ID"
        proj_ref: str = p.get("project", "")
        project_id = proj_ref.split("/", 1)[1] if "/" in proj_ref else proj_ref
        display_name = p.get("displayName") or p.get("name") or project_id
        simplified.append({"project_id": project_id, "display_name": display_name})
    return simplified


def _resources_outside_au(session: AuthorizedSession, org_id: str, allowed_csv: str) -> List[Dict[str, Any]]:
    scope = f"organizations/{org_id}"
    allowed = [x.strip() for x in allowed_csv.split(",") if x.strip()]
    query = _make_non_au_query(allowed)
    results = _cai_search_all_resources(session, scope, query=query, page_size=1000)
    # Keep only fields we care about
    trimmed: List[Dict[str, Any]] = []
    for r in results:
        trimmed.append({
            "name": r.get("name"),
            "assetType": r.get("assetType"),
            "project": r.get("project"),
            "location": r.get("location"),
        })
    return trimmed


def _list_recommenders(session: AuthorizedSession, project_id: str) -> List[str]:
    # Best-effort; some projects may 404 if API not enabled
    url = f"https://recommender.googleapis.com/v1/projects/{project_id}/locations/-/recommenders"
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    if resp.status_code // 100 != 2:
        return []
    data = resp.json()
    names = [r.get("name") for r in data.get("recommenders", []) if r.get("name")]
    return names


def _list_security_recommendations(session: AuthorizedSession, project_id: str) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    names = _list_recommenders(session, project_id)
    for rname in names:
        page_token: Optional[str] = None
        while True:
            url = f"https://recommender.googleapis.com/v1/{rname}/recommendations?pageSize=1000&filter=stateInfo.state=\"ACTIVE\""
            if page_token:
                url += f"&pageToken={page_token}"
            resp = session.get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code // 100 != 2:
                break
            data = resp.json()
            items = data.get("recommendations", [])
            for it in items:
                # Filter to SECURITY
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


def _render_html(org_id: str, projects: List[Dict[str, str]], assets: List[Dict[str, Any]], recs: List[Dict[str, Any]]) -> str:
    date_iso = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    proj_count = len(projects)
    assets_count = len(assets)
    recs_count = len(recs)

    # Summaries
    from collections import Counter
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

    # Optionally show first 100 details
    def _escape(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    projects_rows = "\n".join(
        f"<tr><td>{_escape(p['project_id'])}</td><td>{_escape(p['display_name'])}</td></tr>"
        for p in projects[:100]
    )
    assets_rows = "\n".join(
        f"<tr><td>{_escape(a.get('assetType',''))}</td><td>{_escape(a.get('project',''))}</td><td>{_escape(a.get('location',''))}</td><td><code>{_escape(a.get('name',''))}</code></td></tr>"
        for a in assets[:100]
    )
    recs_rows = "\n".join(
        f"<tr><td>{_escape(r.get('projectId',''))}</td><td>{_escape(r.get('recommenderSubtype',''))}</td><td>{_escape(r.get('priority',''))}</td><td>{_escape(r.get('state',''))}</td><td>{_escape(r.get('lastRefreshTime',''))}</td><td>{_escape(r.get('description',''))}</td></tr>"
        for r in recs[:100]
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
  <div class=\"small\">Allowed AU locations: {ALLOWED_AU_LOCATIONS}</div>
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


def _run_report() -> Dict[str, Any]:
    if not ORG_ID or not BUCKET_NAME:
        raise RuntimeError("Missing ORG_ID or BUCKET_NAME env")

    session = _auth_session()

    projects = _projects_from_cai(session, ORG_ID)

    try:
        assets = _resources_outside_au(session, ORG_ID, ALLOWED_AU_LOCATIONS)
    except Exception:
        assets = []

    recs: List[Dict[str, Any]] = []
    if RECOMMENDER_ENABLED:
        for p in projects:
            pid = p["project_id"]
            try:
                recs.extend(_list_security_recommendations(session, pid))
            except Exception:
                pass

    html = _render_html(ORG_ID, projects, assets, recs)

    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
    object_name = f"gcp_org_security_report_{ts}.html"
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(object_name)
    blob.cache_control = "no-cache"
    blob.content_type = "text/html"
    blob.upload_from_string(html, content_type="text/html")

    return {
        "projects": len(projects),
        "resources_outside_au": len(assets),
        "security_recommendations": len(recs),
        "bucket": BUCKET_NAME,
        "object": object_name,
    }


def generate_report_http(request):  # Optional HTTP entrypoint (not used in secure deployment)
    try:
        result = _run_report()
        result.update({"status": "ok"})
        return (json.dumps(result), 200, {"Content-Type": "application/json"})
    except Exception as e:
        return (str(e), 500)


def generate_report_pubsub(event):  # Secure Pub/Sub entrypoint
    try:
        _run_report()
    except Exception as e:
        # Log to stdout/stderr for Cloud Logging
        print(f"Report generation failed: {e}")
        # Let the function fail to enable retry per trigger policy
        raise
