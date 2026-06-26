"""
One-time setup: creates the Garmin health database in Notion.

Usage:
    python setup_notion.py <parent_page_id>

The parent_page_id is the ID of the Notion page where the database will live.
Get it from the page URL: notion.so/Your-Page-<ID>
"""

import sys
import os
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()


def create_database(notion: Client, parent_page_id: str) -> str:
    db = notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "Garmin Health Log"}}],
        properties={
            "Day": {"title": {}},
            "Date": {"date": {}},
            # Sleep
            "Sleep (hrs)": {"number": {"format": "number"}},
            "Deep Sleep (min)": {"number": {"format": "number"}},
            "Light Sleep (min)": {"number": {"format": "number"}},
            "REM Sleep (min)": {"number": {"format": "number"}},
            "Sleep Score": {"number": {"format": "number"}},
            # Heart
            "Resting HR": {"number": {"format": "number"}},
            "HRV": {"number": {"format": "number"}},
            "HRV Status": {
                "select": {
                    "options": [
                        {"name": "Balanced", "color": "green"},
                        {"name": "Low", "color": "yellow"},
                        {"name": "Unbalanced", "color": "orange"},
                        {"name": "Poor", "color": "red"},
                    ]
                }
            },
            # Workouts
            "Workout Count": {"number": {"format": "number"}},
            "Workouts": {"rich_text": {}},
        },
    )
    return db["id"]


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    parent_page_id = sys.argv[1].replace("-", "")
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("Error: NOTION_TOKEN not set in .env")
        sys.exit(1)

    notion = Client(auth=token)
    db_id = create_database(notion, parent_page_id)
    print(f"\nDatabase created successfully!")
    print(f"Add this to your .env and GitHub secrets:\n")
    print(f"NOTION_DATABASE_ID={db_id}")


if __name__ == "__main__":
    main()
