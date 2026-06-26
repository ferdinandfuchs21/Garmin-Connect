"""
Calculates monthly averages from Garmin Connect and upserts them into Notion.
Usage:
    python sync_monthly_avg.py              # defaults to last full month
    SYNC_MONTH=2026-05 python sync_monthly_avg.py
"""

import os
import json
import calendar
from datetime import date, timedelta
from dotenv import load_dotenv
import requests
import garminconnect

load_dotenv()

# Determine month to sync
sync_month = os.environ.get("SYNC_MONTH")
if sync_month:
    year, month = map(int, sync_month.split("-"))
else:
    today = date.today()
    first = today.replace(day=1)
    last_month = first - timedelta(days=1)
    year, month = last_month.year, last_month.month

start = date(year, month, 1)
end = date(year, month, calendar.monthrange(year, month)[1])
label = start.strftime("%B %Y")
print(f"Syncing monthly averages for {label} ({start} to {end})")


# ---------------------------------------------------------------------------
# Garmin
# ---------------------------------------------------------------------------

def init_garmin():
    api = garminconnect.Garmin(
        email=os.environ["GARMIN_EMAIL"],
        password=os.environ["GARMIN_PASSWORD"],
    )
    api.login()
    return api


def fetch_month(api):
    buckets = {"sleep_hrs": [], "sleep_score": [], "hrv": [], "rhr": []}
    weeks = (end - start).days / 7

    current = start
    while current <= end:
        d = current.isoformat()
        try:
            sleep = api.get_sleep_data(d)
            dto = sleep.get("dailySleepDTO") or {}
            secs = dto.get("sleepTimeSeconds", 0)
            score = ((dto.get("sleepScores") or {}).get("overall") or {}).get("value")
            if secs:
                buckets["sleep_hrs"].append(round(secs / 3600, 2))
            if score:
                buckets["sleep_score"].append(score)
        except Exception:
            pass
        try:
            hrv = api.get_hrv_data(d)
            v = (hrv.get("hrvSummary") or {}).get("lastNight")
            if v:
                buckets["hrv"].append(v)
        except Exception:
            pass
        try:
            rhr_data = api.get_rhr_day(d)
            rhr_list = (
                ((rhr_data.get("allMetrics") or {}).get("metricsMap") or {})
                .get("WELLNESS_RESTING_HEART_RATE") or []
            )
            if rhr_list and rhr_list[0].get("value"):
                buckets["rhr"].append(rhr_list[0]["value"])
        except Exception:
            pass
        current += timedelta(days=1)

    # Fetch all activities for the month in one call
    total_workouts = 0
    total_running_km = 0.0
    total_cycling_km = 0.0
    try:
        acts = api.get_activities_by_date(start.isoformat(), end.isoformat()) or []
        total_workouts = len(acts)
        for act in acts:
            type_key = (act.get("activityType") or {}).get("typeKey", "")
            km = (act.get("distance") or 0) / 1000
            if type_key == "running":
                total_running_km += km
            elif type_key == "cycling":
                total_cycling_km += km
    except Exception:
        pass

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else None

    return {
        "sleep_hrs": avg(buckets["sleep_hrs"]),
        "sleep_score": avg(buckets["sleep_score"]),
        "hrv": avg(buckets["hrv"]),
        "rhr": avg(buckets["rhr"]),
        "weekly_workouts": round(total_workouts / weeks, 2),
        "weekly_running_km": round(total_running_km / weeks, 2),
        "weekly_cycling_km": round(total_cycling_km / weeks, 2),
    }


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

def notion_request(method, path, token, body=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    resp = requests.request(
        method,
        f"https://api.notion.com/v1/{path}",
        headers=headers,
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


def upsert_monthly_page(token, database_id, metrics):
    month_start = start.isoformat()
    month_end = end.isoformat()

    properties = {
        "Day": {"title": [{"text": {"content": f"{label} (avg)"}}]},
        "Date": {"date": {"start": month_start, "end": month_end}},
    }
    if metrics.get("sleep_hrs") is not None:
        properties["Sleep (hrs)"] = {"number": metrics["sleep_hrs"]}
    if metrics.get("sleep_score") is not None:
        properties["Sleep Score"] = {"number": metrics["sleep_score"]}
    if metrics.get("hrv") is not None:
        properties["HRV"] = {"number": metrics["hrv"]}
    if metrics.get("rhr") is not None:
        properties["Resting HR"] = {"number": metrics["rhr"]}
    if metrics.get("weekly_workouts") is not None:
        properties["Weekly Workouts"] = {"number": metrics["weekly_workouts"]}
    if metrics.get("weekly_running_km") is not None:
        properties["Weekly Running (km)"] = {"number": metrics["weekly_running_km"]}
    if metrics.get("weekly_cycling_km") is not None:
        properties["Weekly Cycling (km)"] = {"number": metrics["weekly_cycling_km"]}

    existing = notion_request(
        "POST",
        f"databases/{database_id}/query",
        token,
        body={"filter": {"property": "Day", "title": {"equals": f"{label} (avg)"}}},
    )
    if existing["results"]:
        page_id = existing["results"][0]["id"]
        notion_request("PATCH", f"pages/{page_id}", token, body={"properties": properties})
        print(f"Updated existing Notion page for {label}")
    else:
        notion_request(
            "POST",
            "pages",
            token,
            body={"parent": {"database_id": database_id}, "properties": properties},
        )
        print(f"Created new Notion page for {label}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    api = init_garmin()
    print("Authenticated with Garmin Connect")

    metrics = fetch_month(api)
    print(f"Averages: {json.dumps(metrics, indent=2)}")

    upsert_monthly_page(os.environ["NOTION_TOKEN"], os.environ["NOTION_DATABASE_ID"], metrics)
    print("Done.")


if __name__ == "__main__":
    main()
