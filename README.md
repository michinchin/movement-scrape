# movement-scrape

Scrape gyms in the area to get calendar information.

## Undo an accidental .ics import

Imported events keep a UID ending in `@movement-sync`. To remove only those from your Google Calendar:

1. In Google Cloud console, enable the Calendar API and create an OAuth *Desktop app* client. Save it as `credentials.json` next to the script.
2. `pip install google-api-python-client google-auth-oauthlib`
3. Dry run (lists matches, deletes nothing): `python movement_to_gcal.py --purge-imported`
4. Delete them: `python movement_to_gcal.py --purge-imported --yes`

Use `--calendar <id>` if you imported into a calendar other than your primary one. Deleted events stay in Google Calendar's Trash for 30 days.
