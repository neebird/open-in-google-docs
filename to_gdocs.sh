#!/bin/bash
# Entry point for the Finder Quick Actions "Open in Google Docs" / "Open in Google Sheets"; the work happens in to_gdocs.py.
exec /usr/bin/python3 -I "$(dirname "$0")/to_gdocs.py" "$@"
