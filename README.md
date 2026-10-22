<h1 align="center">Open in Google Docs</h1>

<p align="center">Finder Quick Action for macOS</p>

<p align="center">
  <a href="README.md"><img src="docs/lang-en-active.svg" alt="English" height="30"></a>
  <a href="README.ru.md"><img src="docs/lang-ru.svg" alt="Русский" height="30"></a>
</p>

<p align="center">
  <img src="docs/demo.gif" width="860" alt="Right-click a Word file in Finder, choose Quick Actions → Open in Google Docs, and it opens as a Google Doc">
</p>

Right-click Word, Excel, OpenDocument, RTF or CSV files in Finder and get them as **native Google Docs and Google Sheets**, open in your browser a few seconds later. No Google Drive for desktop, no sync folder, no dragging files into a browser tab.

## What it does

| Quick Action | Files | Becomes |
|---|---|---|
| **↗ Open in Google Docs** | `.docx` `.doc` `.odt` `.rtf` | Google Docs |
| **↗ Open in Google Sheets** | `.xlsx` `.xlsm` `.xls` `.ods` `.csv` `.tsv` | Google Sheets |

- One Google Drive API request per file: upload and conversion together, about 3–5 s per file. Up to 8 files at a time.
- The converted files go to a **From Mac** folder in your Drive.
- Notifications follow your macOS language (English, Russian, Ukrainian) and say what went wrong: password-protected or damaged file, no internet, Drive full, Google not responding.
- If Google goes silent in the middle of an upload, the tool checks whether the file was created anyway, opens it if so, and otherwise retries once.
- Nothing to install from pip: only Python's standard library on the system `/usr/bin/python3`.

## Privacy

- Files go straight from your Mac to your own Google Drive. There is no server in between and no telemetry.
- The tool uses the `drive.file` scope, so it can only see files it created itself, not the rest of your Drive.
- Your OAuth credentials stay in rclone's config (`~/.config/rclone/rclone.conf`). The access token is cached for an hour in `~/Library/Caches/to_gdocs/token.json`, readable only by you.

## Requirements

- macOS 12 Monterey or later.
- Xcode Command Line Tools, which provide `/usr/bin/python3`: `xcode-select --install`.
- [rclone](https://rclone.org), used only to sign in to Google: `brew install rclone`.
- A Google account and your own free Google Cloud OAuth client (step 1 below).

## Setup

### 1. Create your Google OAuth client (once, about 5 minutes)

Google wants every app to have its own OAuth client, and rclone's shared client is being retired, so create one:

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create a project (any name).
2. **APIs & Services → Library**: find **Google Drive API** and click **Enable**.
3. **Google Auth Platform** (formerly *OAuth consent screen*): click **Get started**, enter an app name (for example "Open in Google Docs") and your email, choose **External**.
4. **Data Access → Add or remove scopes**: add `https://www.googleapis.com/auth/drive.file` and save.
5. **Audience → Test users → Add users**: add your own Google account.
6. **Clients → Create client → Desktop app**. Copy the **Client ID** and the **Client secret**.
7. Optional: **Audience → Publish app**. While the app is in *Testing*, Google asks you to sign in again every 7 days; the tool then opens the sign-in page for you. If **Publish app** is greyed out, Google first wants a home page and a privacy policy link: fill them in under **Branding** (any page you control works, for example a free GitHub Pages site), add that site's domain under **Authorized domains**, save, and publish. The `drive.file` scope is not a sensitive scope, so Google doesn't need to review the app.

### 2. Connect rclone to your Drive

```sh
rclone config create gdrive drive client_id=YOUR_CLIENT_ID client_secret=YOUR_CLIENT_SECRET scope=drive.file
```

A browser window opens: sign in and click **Allow**. If Google says the app isn't verified, choose **Advanced → Go to …**; it is your own app.

### 3. Install the Quick Actions

Download or clone this repository, open Terminal in its folder and run:

```sh
./install.sh
```

It copies the two Quick Actions to `~/Library/Services` and points them at this folder. Run it again if you move the folder.

### 4. Use it

Select files in Finder → right-click → **Quick Actions** → **↗ Open in Google Docs** or **↗ Open in Google Sheets**.

If the items don't show up, right-click → **Quick Actions → Customize…** and turn them on, or look under **Services** in the same menu.

## Good to know

- **CSV/TSV are parsed the US way by Google.** Decimal commas (`1,5`) and dates like `21.10.2026` stay text, and digit-only codes lose leading zeros. Delimiters and encodings (`;`, tabs, Windows-1251) are detected fine. Use `.xlsx` when exact values matter.
- **Select documents and spreadsheets separately.** macOS offers a Quick Action only when every selected file matches it.
- **Password-protected files** can't be converted by Google. Remove the password first.
- **Google's import limits apply**, for example 100 MB for spreadsheets.
- **Apple Numbers and Pages files** aren't supported because Google can't import them. Export them to Excel or Word first.
- In a rare case, Google finishes a stalled upload after the retry has started, and you get two copies of that file.

## Troubleshooting

Every run is logged to `~/Library/Logs/to_gdocs.log`, including Google's own reason for any error.

| Notification | What to do |
|---|---|
| Google Drive isn't set up yet | Do setup steps 1–2. The rclone remote must be called `gdrive` and use your own client ID. The tool reads `~/.config/rclone/rclone.conf` (or the legacy `~/.rclone.conf`; check with `rclone config file`) and can't read a password-encrypted rclone config. |
| rclone isn't installed | `brew install rclone` |
| Sign in to Google again | A browser page opens: click **Allow** and run the Quick Action again. If this happens every week, publish your app (step 1.7). |
| Google couldn't open the file | The file is probably password-protected. |
| Google couldn't read the file | The file is empty or damaged. |
| Google didn't respond | Try again. The tool already retried once. |

## Configuration

Edit the constants at the top of `to_gdocs.py`:

- `REMOTE`: the rclone remote name.
- `FOLDER_NAME`: the Drive folder. The default is "From Mac" in your macOS language.
- `STALL_TIMEOUT`: how long Google may stay silent before the tool checks and retries.

## Uninstall

```sh
./uninstall.sh
```

It removes the Quick Actions and the cached token. It keeps your rclone remote, the log and your files in Google Drive.

## How it works

`to_gdocs.sh` runs `/usr/bin/python3 -I to_gdocs.py` with the selected files. For each file, the script:

1. Uses the cached access token, or gets a new one with the refresh token from your rclone remote.
2. Sends one multipart upload with the target Google type in the metadata, so Drive converts the file on upload.
3. Opens the edit URL of the new document in your default browser.

Every upload carries a random tag in its `appProperties`. After a timeout, that tag lets the tool find the file if Google created it anyway, instead of uploading it twice.

## Development

```sh
/usr/bin/python3 -m unittest discover -s tests -v
```

Offline tests run against a fake Drive server. You don't need a Google account to run them.

## License

[MIT](LICENSE)
