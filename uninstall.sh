#!/bin/bash
# Removes every Quick Action that runs to_gdocs.sh and the cached access token.
# Keeps the rclone remote, the log and everything already uploaded to Google Drive.
set -euo pipefail

for wf in "$HOME/Library/Services"/*.workflow; do
  [ -d "$wf" ] || continue
  case "$(plutil -extract actions.0.action.ActionParameters.COMMAND_STRING raw -o - \
          "$wf/Contents/Resources/document.wflow" 2>/dev/null || true)" in
    *to_gdocs.sh*) rm -rf "$wf"; echo "Removed: $(basename "$wf" .workflow)" ;;
  esac
done
/System/Library/CoreServices/pbs -update
rm -rf "$HOME/Library/Caches/to_gdocs"
echo "Removed the cached access token. Kept: the rclone remote \"gdrive\", ~/Library/Logs/to_gdocs.log and your files in Google Drive."
