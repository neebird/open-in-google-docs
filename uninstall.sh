#!/bin/bash
# Removes the two Quick Actions that install.sh installed and the cached access token.
# Keeps the rclone remote, the log and everything already uploaded to Google Drive.
set -euo pipefail

here="$(CDPATH= cd "$(dirname "$0")" && pwd)"
ours="/to_gdocs.sh' \"\$@\""  # how every command written by install.sh ends
for src in "$here"/quick-actions/*.workflow; do
  name="$(basename "$src" .workflow)"
  wf="$HOME/Library/Services/$name.workflow"
  [ -d "$wf" ] || continue
  case "$(plutil -extract actions.0.action.ActionParameters.COMMAND_STRING raw -o - \
          "$wf/Contents/Resources/document.wflow" 2>/dev/null || true)" in
    *"$ours"|*"Run ./install.sh from the repository"*) rm -rf "$wf"; echo "Removed: $name" ;;
  esac
done
/System/Library/CoreServices/pbs -update
rm -rf "$HOME/Library/Caches/to_gdocs"
echo "Removed the cached access token. Kept: the rclone remote \"gdrive\", ~/Library/Logs/to_gdocs.log and your files in Google Drive."
