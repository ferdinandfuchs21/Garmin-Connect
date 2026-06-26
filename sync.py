"""
Pulls yesterday's Garmin Connect data and upserts it into a Notion database.

Required environment variables:
    GARMIN_EMAIL, GARMIN_PASSWORD  — Garmin Connect credentials
    NOTION_TOKEN                   — Notion integration token
    NOTION_DATABASE_ID             — ID of the Garmin Health Log database

Optional:
    GARTH_TOKENS  — Base64-encoded tarball of ~/.garth (used in GitHub Actions
                    to avoid re-authenticating every run)
    SYNC_DATE     — Override the date to sync (YYYY-MM-DD), defaults to yesterday
"""

import os
import base64
import json
import subprocess
import tempfile
from datetime import date, timedelta

import requests
import garminconnect
from dotenv import load_dotenv

load_dotenv()

SYNC_DATE = os.environ.get("SYNC_DATE") or (date.today() - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Garmin authentication
# ---------------------------------------------------------------------------

def _restore_garth_tokens():
    tokens_b64 = os.environ.get("GARTH_TOKENS", "").strip()
    if not tokens_b64:
        return
    garth_home = os.path.expanduser("~/.garth")
    os.makedirs(garth_home, exist_ok=True)
    token_bytes = base64.b64decode(tokens_b64)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
        tmp.write(token_bytes)
        tmp_path = tmp.name
    subprocess.run(["tar", "-xzf", tmp_path, "-C", "/"], check=False)
    os.unlink(tmp_path)


def init_garmin() -> garminconnect.Garmin:
    _restore_garth_tokens()
    api = garminconnect.Garmin(
        email=os.environ["GARMIN_EMAIL"],
        password=os.environ["GARMIN_PASSWORD"],
        is_cn=False,
    )
    api.login()
    return api


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_sleep(api: garminconnect.Garmin) -> dict:
    try:
        data = api.get_sleep_data(SYNC_DATE)
        dto = data.get("dailySleepDTO") or {}
        scores = dto.get("sleepScores") or {}
        overall = scores.get("overall") or {}
        return {
            "sleep_hrs": round(dto.get("sleepTimeSeconds", 0) / 3600, 2) or None,
            "deep_min": round(dto.get("deepSleepSeconds", 0) / 60) or None,
            "light_min": round(dto.get("lightSleepSeconds", 0) / 60) or None,
            "rem_min": round(dto.get("remSleepSeconds", 0) / 60) or None,
            "sleep_score": overall.get("value"),
        }
    except Exception as e:
        print(f"Warning: could not fetch sleep data: {e}")
        return {}


def fetch_hrv(api: garminconnect.Garmin) -> dict:
    try:
        data = api.get_hrv_data(SYNC_DATE)
        summary = data.get("hrvSummary") or {}
        raw_status = summary.get("status", "")
        status = raw_status.replace("_", " ").title() if raw_status else None
        return {
            "hrv": summary.get("lastNight"),
            "hrv_status": status,
        }
    except Exception as e:
        print(f"Warning: could not fetch HRV data: {e}")
        return {}


def fetch_rhr(api: garminconnect.Garmin) -> dict:
    try:
        data = api.get_rhr_day(SYNC_DATE)
        metrics_map = (data.get("allMetrics") or {}).get("metricsMap") or {}
        rhr_list = metrics_map.get("WELLNESS_RESTING_HEART_RATE") or []
        value = rhr_list[0].get("value") if rhr_list else None
        return {"rhr": int(value) if value else None}
    except Exception as e:
        print(f"Warning: could not fetch RHR data: {e}")
        return {}


def fetch_workouts(api: garminconnect.Garmin) -> dict:
    try:
        activities = api.get_activities_by_date(SYNC_DATE, SYNC_DATE) or []
        lines = []
        for act in activities:
            name = act.get("activityName") or (act.get("activityType") or {}).get("typeKey", "Activity")
            duration_min = round((act.get("duration") or 0) / 60)
            distance_km = round((act.get("distance") or 0) / 1000, 2)
            avg_hr = act.get("averageHR")

            parts = [name, f"{duration_min} min"]
            if distance_km:
                parts.append(f"{distance_km} km")
            if avg_hr:
                parts.append(f"avg {int(avg_hr)} bpm")
            lines.append(" · ".join(parts))

        return {
            "workout_count": len(lines),
            "workouts_text": "\n".join(lines),
        }
    except Exception as e:
        print(f"Warning: could not fetch workout data: {e}")
        return {"workout_count": 0, "workouts_text": ""}


# ---------------------------------------------------------------------------
# Notion upsert
# ---------------------------------------------------------------------------

def _num(value) -> dict:
    return {"number": value}


def _text(value: str) -> dict:
    content = (value or "")[:2000]
    return {"rich_text": [{"text": {"content": content}}]}


def _select(name) -> dict:
    if not name:
        return {"select": None}
    return {"select": {"name": name}}


def build_properties(metrics: dict, formatted_day: str) -> dict:
    props = {
        "Day": {"title": [{"text": {"content": formatted_day}}]},
        "Date": {"date": {"start": SYNC_DATE}},
        "Sleep (hrs)": _num(metrics.get("sleep_hrs")),
        "Deep Sleep (min)": _num(metrics.get("deep_min")),
        "Light Sleep (min)": _num(metrics.get("light_min")),
        "REM Sleep (min)": _num(metrics.get("rem_min")),
        "Sleep Score": _num(metrics.get("sleep_score")),
        "Resting HR": _num(metrics.get("rhr")),
        "HRV": _num(metrics.get("hrv")),
        "HRV Status": _select(metrics.get("hrv_status")),
        "Workout Count": _num(metrics.get("workout_count")),
        "Workouts": _text(metrics.get("workouts_text", "")),
    }
    # Drop number properties that have no value to avoid Notion validation errors
    return {
        k: v for k, v in props.items()
        if not ("number" in v and v["number"] is None)
    }


def notion_request(method, path, token, body=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    resp = requests.request(
        method,
        f"https://api.notionhq.com/v1/{path}",
        headers=headers,
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


def upsert_page(token: str, database_id: str, metrics: dict):
    from datetime import datetime
    day_obj = datetime.strptime(SYNC_DATE, "%Y-%m-%d")
    formatted_day = day_obj.strftime("%a, %d %b %Y")

    properties = build_properties(metrics, formatted_day)

    existing = notion_request(
        "POST",
        f"databases/{database_id}/query",
        token,
        body={"filter": {"property": "Date", "date": {"equals": SYNC_DATE}}},
    )

    if existing["results"]:
        page_id = existing["results"][0]["id"]
        notion_request("PATCH", f"pages/{page_id}", token, body={"properties": properties})
        print(f"Updated existing Notion page for {SYNC_DATE}")
    else:
        notion_request(
            "POST",
            "pages",
            token,
            body={"parent": {"database_id": database_id}, "properties": properties},
        )
        print(f"Created new Notion page for {SYNC_DATE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Syncing Garmin data for {SYNC_DATE}...")

    api = init_garmin()
    print("Authenticated with Garmin Connect")

    metrics: dict = {}
    metrics.update(fetch_sleep(api))
    metrics.update(fetch_hrv(api))
    metrics.update(fetch_rhr(api))
    metrics.update(fetch_workouts(api))

    print(f"Fetched: {json.dumps(metrics, indent=2)}")

    upsert_page(os.environ["NOTION_TOKEN"], os.environ["NOTION_DATABASE_ID"], metrics)
    print("Done.")


if __name__ == "__main__":
    main()
