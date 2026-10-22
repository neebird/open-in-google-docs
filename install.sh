#!/bin/bash
# Installs the Finder Quick Actions "Open in Google Docs" and "Open in Google Sheets" for this
# checkout: copies them to ~/Library/Services, points them at to_gdocs.sh in this folder and
# refreshes the Services menu. Re-run it after moving the folder.
set -euo pipefail

here="$(CDPATH= cd "$(dirname "$0")" && pwd)"
[ -d "$here/quick-actions" ] || { echo "quick-actions/ not found next to install.sh" >&2; exit 1; }
services="$HOME/Library/Services"
wflow="Contents/Resources/document.wflow"
key="actions.0.action.ActionParameters.COMMAND_STRING"
# The Quick Action runs this shell command; quote the path for bash (it may contain spaces or ').
command="'$(printf '%s' "$here/to_gdocs.sh" | sed "s/'/'\\\\''/g")' \"\$@\""
ours="/to_gdocs.sh' \"\$@\""  # how every command written by install.sh ends, whatever the folder

mkdir -p "$services"
for src in "$here"/quick-actions/*.workflow; do
  name="$(basename "$src" .workflow)"
  dst="$services/$name.workflow"
  if [ -e "$dst" ]; then
    case "$(plutil -extract "$key" raw -o - "$dst/$wflow" 2>/dev/null || true)" in
      *"$ours"|*"Run ./install.sh from the repository"*) rm -rf "$dst" ;;  # an earlier install or a double-clicked copy
      *) mkdir -p "$HOME/.Trash"
         mv "$dst" "$HOME/.Trash/$name $(date +%Y%m%d-%H%M%S).workflow"
         echo "Moved your own Quick Action with the same name to the Trash: $name" ;;
    esac
  fi
  cp -R "$src" "$dst"
  plutil -replace "$key" -string "$command" "$dst/$wflow"
  echo "Installed: $name"
done
chmod +x "$here/to_gdocs.sh"
/System/Library/CoreServices/pbs -update

if ! xcode-select -p >/dev/null 2>&1; then
  echo "Next: install the Xcode Command Line Tools for /usr/bin/python3: xcode-select --install"
fi
if ! { [ -x /opt/homebrew/bin/rclone ] || [ -x /usr/local/bin/rclone ] || [ -x /opt/local/bin/rclone ] || command -v rclone >/dev/null; }; then
  echo "Next: install rclone (brew install rclone) and set up the \"gdrive\" remote, see README.md"
elif ! grep -qs '^\[gdrive\]' "$HOME/.config/rclone/rclone.conf" "$HOME/.rclone.conf"; then
  echo "Next: set up the rclone remote \"gdrive\" in ~/.config/rclone/rclone.conf with your own Google OAuth client, see README.md"
fi
echo "Done. In Finder: select files, right-click, Quick Actions."
