#!/usr/bin/env python3
"""ap-digitals integration status collector.

Reads last-data date from 4 sources via service account, computes
status (GREEN/YELLOW/RED by freshness), writes platform|status|last_date
into the public status spreadsheet. GH Pages widget reads that sheet.

Status thresholds (days since last data):
  GREEN  <= 2
  YELLOW 3-7
  RED    > 7
"""
import json
import os
import ssl
import urllib.request
import urllib.error
from datetime import date, timedelta

import certifi
from google.oauth2 import service_account
import google.auth.transport.requests

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(SCRIPT_DIR, "client.json")

GA4_PROPERTY = "517231269"
GSC_SITE = "sc-domain:ap-digitals.com"
STATUS_SHEET_ID = "1ZrV1gk284yxzwgNLM6ZlwQcLHUVDVL3BunKVZsXfIRQ"
STATUS_SHEET_RANGE = "status!A2:C"  # sheet named 'status'?

FB_SHEET = "1N7qiagiwwIP3UKnHD_oA2kNPwxHfTwOphaygJn5KLQs"
IG_SHEET = "1iKW-esn69suzL1Da87OqADBT7NLPVU1Rl7gE5n9dMdc"
# Public sheets (readable via gviz without auth). Column A holds the date.
LINKEDIN_SHEET = "1nbzGBt53MuiVFNxNVPPP_rwP4AYC_tFfiZyWW2fHovg"
CRM_SHEET = "1KpiWUxu2k3SEwfseflAK_sQI5sLh3JxHj2KLLrmzbS8"
# Shared to the service account (auth via Sheets API, like FB/IG). Column A = date.
YOUTUBE_SHEET = "1LYiIPDv4rTPRU-MJlLkMW8ELPUq-w000lBq36S52C7M"
# SendPulse collector writes into this sheet (Campaigns + LastUpdate tabs).
SENDPULSE_SHEET = "1D0ea1XT3FlCD4iK9gbiRzCuqtIobdGx06U0N6BlmPes"
# Google Ads (lazy; needs google-ads lib + secrets from env). Empty when unset.
GADS = {
    "dev_token": os.environ.get("GADS_DEV_TOKEN", ""),
    "client_id": os.environ.get("GADS_CLIENT_ID", ""),
    "client_secret": os.environ.get("GADS_CLIENT_SECRET", ""),
    "refresh_token": os.environ.get("GADS_REFRESH_TOKEN", ""),
    "manager_id": os.environ.get("GADS_MANAGER_ID", ""),
    "customer_id": os.environ.get("GADS_CUSTOMER_ID", ""),
}
# Windsor.ai (Meta Ads via 'facebook' connector). Empty when unset.
WINDSOR_API_KEY = os.environ.get("WINDSOR_API_KEY", "")
WINDSOR_FB_ACCOUNT = os.environ.get("WINDSOR_FB_ACCOUNT", "facebook__1900818654526204")

GREEN_DAYS = 2
YELLOW_DAYS = 7
# GSC data lags ~1-2 days behind GA4 (Google-side), so use a relaxed
# GREEN threshold to avoid a permanent YELLOW from normal freshness.
GSC_GREEN_DAYS = 3


def creds(scopes):
    c = service_account.Credentials.from_service_account_file(CLIENT, scopes=scopes)
    c.refresh(google.auth.transport.requests.Request())
    return c.token


def api(url, token, body=None, method=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def ga4_last_date(tok):
    body = {
        "dateRanges": [{"startDate": "365daysAgo", "endDate": "today"}],
        "metrics": [{"name": "sessions"}],
        "dimensions": [{"name": "date"}],
        "orderBys": [{"dimension": {"dimensionName": "date", "orderType": "NUMERIC"}, "desc": True}],
        "limit": 1,
    }
    code, d = api(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY}:runReport",
        tok, body,
    )
    if code != 200 or "rows" not in d:
        raise RuntimeError(f"GA4: HTTP {code} {d.get('error', d)}")
    raw = d["rows"][0]["dimensionValues"][0]["value"]  # YYYYMMDD
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))


def gsc_last_date(tok):
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=365)).isoformat()
    body = {"startDate": start, "endDate": end, "dimensions": ["date"], "rowLimit": 25000}
    code, d = api(
        f"https://www.googleapis.com/webmasters/v3/sites/{GSC_SITE}/searchAnalytics/query",
        tok, body,
    )
    if code != 200 or "rows" not in d:
        raise RuntimeError(f"GSC: HTTP {code} {d.get('error', d)}")
    dates = [r["keys"][0] for r in d["rows"]]
    if not dates:
        raise RuntimeError("GSC: no rows")
    return date.fromisoformat(max(dates))


def gads_last_date():
    """Last date Google Ads has data for this customer, via Google Ads API.

    Needs the google-ads library and all GADS_* env secrets. Queries the
    most recent day with impressions within the last 400 days; a day counts
    only if it has data (metrics.impressions > 0).
    """
    if not all(GADS[k] for k in ("dev_token", "client_id", "client_secret",
                                 "refresh_token", "manager_id", "customer_id")):
        raise RuntimeError("GADS_* env not fully set")
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError as e:
        raise RuntimeError(f"google-ads lib missing: {e}")
    conf = {
        "developer_token": GADS["dev_token"],
        "client_id": GADS["client_id"],
        "client_secret": GADS["client_secret"],
        "refresh_token": GADS["refresh_token"],
        "login_customer_id": GADS["manager_id"],  # MCC manager
        "use_proto_plus": True,
    }
    client = GoogleAdsClient.load_from_dict(conf)
    svc = client.get_service("GoogleAdsService")
    cid = GADS["customer_id"]
    start = (date.today() - timedelta(days=400)).isoformat()
    end = date.today().isoformat()
    query = (
        f"SELECT segments.date FROM customer "
        f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
        f"AND metrics.impressions > 0 "
        f"ORDER BY segments.date DESC LIMIT 1"
    )
    rows = list(svc.search(customer_id=cid, query=query))
    if not rows:
        raise RuntimeError("Google Ads: no rows with data in last 400 days")
    return date.fromisoformat(rows[0].segments.date)


def meta_last_date():
    """Last date Meta Ads has data via Windsor.ai 'facebook' connector.

    Queries a wide date window, grabs the newest distinct date present. Raises
    when Windsor has no data yet (caller renders a grey row).
    """
    if not WINDSOR_API_KEY:
        raise RuntimeError("WINDSOR_API_KEY not set")
    today = date.today()
    fromd = (today - timedelta(days=400)).isoformat()
    params = {
        "api_key": WINDSOR_API_KEY,
        "fields": "date,spend",
        "date_from": fromd,
        "date_to": today.isoformat(),
        "_max_rows": "1000",
    }
    url = "https://connectors.windsor.ai/facebook?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={"User-Agent": "Windsor/1.0"})
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=30) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Windsor facebook: HTTP {e.code} {e.read().decode()[:200]}")
    rows = payload.get("data") or []
    dates = set()
    for row in rows:
        ds = str(row.get("date", ""))[:10]
        try:
            dates.add(date.fromisoformat(ds))
        except ValueError:
            continue
    if not dates:
        raise RuntimeError("Windsor: no Meta data in last 400 days")
    return max(dates)


def sheet_last_date(sheet_id, tok):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1:A10000"
    code, d = api(url, tok)
    if code != 200 or "values" not in d:
        raise RuntimeError(f"Sheets {sheet_id}: HTTP {code} {d.get('error', d)}")
    dates = [r[0] for r in d["values"] if r and r[0].strip().lower() not in ("date", "")]
    if not dates:
        raise RuntimeError(f"Sheets {sheet_id}: no dates")
    return date.fromisoformat(max(dates))


def public_sheet_last_date(sheet_id):
    """Read a public sheet's column A via gviz (no auth). Column A = date."""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=20) as r:
        text = r.read().decode("utf-8", "ignore")
    dates = []
    for line in text.splitlines()[1:]:  # skip header
        if not line.strip():
            continue
        d = line.split(",")[0].strip().strip('"')[:10]
        try:
            dates.append(date.fromisoformat(d))
        except ValueError:
            pass
    if not dates:
        raise RuntimeError(f"Public sheet {sheet_id}: no dates")
    return max(dates)


def sendpulse_last_date(tok):
    """Freshness = last successful SendPulse collector run, per LastUpdate log."""
    code, d = api(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SENDPULSE_SHEET}/values/LastUpdate!A1:D200",
        tok,
    )
    if code != 200:
        raise RuntimeError(f"SendPulse LastUpdate: HTTP {code}")
    latest = None
    for r in reversed(d.get("values", [])):
        if len(r) >= 3 and r[1] == "Campaigns" and r[2] == "OK":
            ts = r[0][:10]  # YYYY-MM-DD
            try:
                latest = date.fromisoformat(ts)
                break
            except ValueError:
                continue
    if latest is None:
        raise RuntimeError("SendPulse: no successful run logged")
    return latest


def status_for(d, today, green_days=GREEN_DAYS):
    days = (today - d).days
    if days <= green_days:
        return "GREEN"
    if days <= YELLOW_DAYS:
        return "YELLOW"
    return "RED"


def main():
    today = date.today()
    tok = creds(["https://www.googleapis.com/auth/analytics.readonly",
                 "https://www.googleapis.com/auth/webmasters.readonly",
                 "https://www.googleapis.com/auth/spreadsheets"])

    rows = []
    # GA4
    try:
        d = ga4_last_date(tok)
        rows.append(["GA4", status_for(d, today), d.isoformat()])
    except Exception as e:
        rows.append(["GA4", "RED", f"ERR {e}"])
    # GSC
    try:
        d = gsc_last_date(tok)
        rows.append(["GSC", status_for(d, today, GSC_GREEN_DAYS), d.isoformat()])
    except Exception as e:
        rows.append(["GSC", "RED", f"ERR {e}"])
    # FB / IG
    for name, sid in (("Facebook", FB_SHEET), ("Instagram", IG_SHEET)):
        try:
            d = sheet_last_date(sid, tok)
            rows.append([name, status_for(d, today), d.isoformat()])
        except Exception as e:
            rows.append([name, "RED", f"ERR {e}"])
    # LinkedIn / CRM (public sheets, column A = date)
    for name, sid in (("LinkedIn", LINKEDIN_SHEET), ("CRM", CRM_SHEET)):
        try:
            d = public_sheet_last_date(sid)
            rows.append([name, status_for(d, today), d.isoformat()])
        except Exception as e:
            rows.append([name, "RED", f"ERR {e}"])
    # YouTube (service-account sheet, column A = date)
    try:
        d = sheet_last_date(YOUTUBE_SHEET, tok)
        rows.append(["YouTube", status_for(d, today), d.isoformat()])
    except Exception as e:
        rows.append(["YouTube", "RED", f"ERR {e}"])
    # SendPulse: freshness = last collector run, but only once real data exists.
    # Until a campaign appears this stays grey ('немає даних').
    try:
        d = sendpulse_last_date(tok)
        rows.append(["SendPulse", status_for(d, today), d.isoformat()])
    except Exception as e:
        rows.append(["SendPulse", "—", "немає даних"])
    # Google Ads (via API when secrets set, else grey); Meta Ads via Windsor.
    try:
        d = gads_last_date()
        rows.append(["Google Ads", status_for(d, today), d.isoformat()])
    except Exception as e:
        rows.append(["Google Ads", "—", "немає даних"])
    try:
        d = meta_last_date()
        rows.append(["Meta Ads", status_for(d, today), d.isoformat()])
    except Exception as e:
        rows.append(["Meta Ads", "—", "немає даних"])

    # write to status sheet
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{STATUS_SHEET_ID}/values/status!A2:C?valueInputOption=RAW"
    code, d = api(url, tok, {"range": "status!A2:C", "majorDimension": "ROWS", "values": rows}, method="PUT")
    if code != 200:
        raise RuntimeError(f"Write status sheet: HTTP {code} {d.get('error', d)}")

    # Watchdog output: stay silent when all GREEN, emit only non-GREEN lines
    # so the cron delivery only fires on a problem.
    for r in rows:
        if r[1] != "GREEN" or r[2].startswith("ERR"):
            print("\t".join(r))


if __name__ == "__main__":
    main()
