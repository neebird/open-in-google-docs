#!/bin/bash
# Installs the Finder Quick Actions "Open in Google Docs" and "Open in Google Sheets" for this
# checkout: copies them to ~/Library/Services, points them at to_gdocs.sh in this folder and
# refreshes the Services menu. Re-run it after moving the folder.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
services="$HOME/Library/Services"
wflow="Contents/Resources/document.wflow"
key="actions.0.action.ActionParameters.COMMAND_STRING"

mkdir -p "$services"
for wf in "$services"/*.workflow; do
  [ -d "$wf" ] || continue
  case "$(plutil -extract "$key" raw -o - "$wf/$wflow" 2>/dev/null || true)" in
    *to_gdocs.sh*) ;;
    *) continue ;;
  esac
  name="$(basename "$wf")"
  if [ -d "$here/quick-actions/$name" ]; then
    rm -rf "$wf"  # our own earlier install: replaced below
  else
    mkdir -p "$HOME/.Trash"
    mv "$wf" "$HOME/.Trash/${name%.workflow} $(date +%Y%m%d-%H%M%S).workflow"
    echo "Moved an older Quick Action that ran to_gdocs.sh to the Trash: ${name%.workflow}"
  fi
done

# The Quick Action runs this shell command; quote the path for bash (it may contain spaces or ').
command="'$(printf '%s' "$here/to_gdocs.sh" | sed "s/'/'\\\\''/g")' \"\$@\""
for src in "$here"/quick-actions/*.workflow; do
  dst="$services/$(basename "$src")"
  cp -R "$src" "$dst"
  plutil -replace "$key" -string "$command" "$dst/$wflow"
  echo "Installed: $(basename "$src" .workflow)"
done
chmod +x "$here/to_gdocs.sh"
/System/Library/CoreServices/pbs -update

if ! xcode-select -p >/dev/null 2>&1; then
  echo "Next: install the Xcode Command Line Tools for /usr/bin/python3: xcode-select --install"
fi
if ! { [ -x /opt/homebrew/bin/rclone ] || [ -x /usr/local/bin/rclone ] || [ -x /opt/local/bin/rclone ] || command -v rclone >/dev/null; }; then
  echo "Next: install rclone (brew install rclone) and set up the \"gdrive\" remote, see README.md"
elif ! grep -q '^\[gdrive\]' "$HOME/.config/rclone/rclone.conf" 2>/dev/null; then
  echo "Next: set up the rclone remote \"gdrive\" with your own Google OAuth client, see README.md"
fi
echo "Done. In Finder: select files, right-click, Quick Actions."
