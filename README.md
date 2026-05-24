# codex switch

This project now runs as a small Electron shell with a Python backend and a React frontend for switching between isolated Codex accounts.

It does:

- discover accounts only from `~/llm_accounts_profiles/codex/profiles` and cache metadata in `~/codex_switch_data/accounts.json`
- start new local accounts with the same pending browser sign-in flow pattern used by `flutty_orc`
- persist newly added accounts into `~/codex_switch_data/accounts.json` only after the ChatGPT sign-in completes
- create isolated account homes and copied credential files directly under `~/llm_accounts_profiles/codex/profiles/<account-id>/home`
- render the UI from `~/codex_switch_data`
- persist its own primary-account selection in `~/codex_switch_data/config.json`
- copy the selected account's `.codex/auth.json` into the main Codex home at `~/.codex/auth.json` when Set Primary is clicked
- merge live account rate-limit usage from a running `flutty_orc` instance when available
- map accounts to prepared isolated Codex user-data directories under `~/llm_accounts_profiles/codex/profiles`
- auto-mark newly connected local accounts as primary in the switcher
- launch a selected account in an isolated Codex app profile with `--user-data-dir=<prepared dir>` without closing the current Codex session
- reopen the main Codex Desktop profile without closing isolated Codex app profiles
- force normal Codex Desktop launches to the main `~/.codex` workspace state, not an isolated account's `home/.codex` state

It does not:

- copy browser cookies, local storage, or Keychain items
- write into the default Codex Electron user-data profile under `~/Library/Application Support/Codex`

## Install and Run

```bash
cd /Users/liorhadad/codex_switch
npm install
npm run electron
```

## Build Downloadable Apps

The app is packaged with Electron Builder. The React UI is compiled into `web/dist`, the Python backend is compiled into a standalone executable with PyInstaller, `npm run dist` writes generic output to `release/`, and the platform-specific installer commands write to `mac-installer/` and `windows-installer/`.

Install the build tools once:

```bash
cd /Users/liorhadad/codex_switch
npm install
python3 -m pip install pyinstaller
```

Build the Mac app on macOS:

```bash
npm run dist:mac
```

Build the Windows app on Windows:

```powershell
npm run dist:win
```

You normally cannot build the Windows backend executable correctly from macOS. Use a Windows machine, or run the included GitHub Actions workflow from the Actions tab to build both macOS and Windows artifacts.

If PyInstaller is installed under a specific Python version, pass it explicitly:

```bash
PYTHON=python3.12 npm run dist:mac
```

## Auto Updates

The packaged desktop app now supports in-app update checks using `electron-updater` with Cloudflare R2 as the generic static host. This works well with a private source repo because only the built artifacts need to be published.

This repo is now wired to publish updater files to:

- bucket: `codex-switch-updates`
- public URL: `https://pub-1fc6be6e977a4adf8a928d5e615d8f54.r2.dev`

Release publishing uploads:

- `latest.yml`
- `latest-mac.yml`
- generated `.exe`, `.zip`, `.dmg`, and `.blockmap` artifacts

Publish the generated installer outputs to R2 with:

```bash
npm run publish:updates
```

The GitHub Actions workflow builds both macOS and Windows apps and then publishes the resulting updater artifacts to R2. It runs on:

- pushes to `main`
- `v*` tags
- manual runs from the Actions tab

For normal `main` commits and manual runs, the workflow stamps the build with a unique updater version using the package major/minor plus the GitHub run number so Electron clients see it as a newer release. For `v*` tags, the tag version is used. Add this repository secret before using the workflow:

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`

macOS updater releases must be signed with an Apple Developer ID Application certificate and notarized through Apple. Without stable Developer ID signing, Squirrel/Mac rejects `Restart to Update` with a code-signature validation error because each ad-hoc build has a different code hash. Without notarization, downloaded builds can trigger Gatekeeper warnings that macOS cannot verify the app. The workflow imports `CSC_LINK` into a temporary keychain, refuses self-signed/ad-hoc release builds, notarizes through App Store Connect, and validates the stapled app before publishing update artifacts. Add these repository secrets before publishing macOS updates:

- `CSC_LINK`: base64-encoded exported code-signing `.p12`, or a private URL to it
- `CSC_KEY_PASSWORD`: password for the exported `.p12`
- `APPLE_API_KEY_P8`: contents of the App Store Connect team API key `.p8`
- `APPLE_API_KEY_ID`: App Store Connect API key ID
- `APPLE_API_ISSUER`: App Store Connect issuer ID

Instead of `APPLE_API_KEY_P8`, you may set `APPLE_API_KEY_BASE64` to a base64-encoded `.p8` file. Create the key in App Store Connect under Users and Access -> Integrations -> App Store Connect API, choose a Team Key, and grant App Manager access. The legacy Apple ID notarization path is still supported with these secrets if an API key is not configured:

- `APPLE_ID`
- `APPLE_APP_SPECIFIC_PASSWORD`
- `APPLE_TEAM_ID`

If users already installed an ad-hoc-signed build, they may need one manual reinstall from the first stably signed `.dmg`; future updates signed by the same certificate can then install normally.

You can still override the defaults if you move to another bucket or custom domain:

```bash
CODEX_SWITCH_R2_BUCKET=another-bucket \
CODEX_SWITCH_UPDATE_BASE_URL=https://downloads.example.com \
npm run publish:updates
```

The app checks for updates on launch, shows an update card inside the UI, downloads on demand, and then restarts into the new version after `Restart to Update`.

## License Activation

Codex Switch now supports a remote one-installation-per-key activation gate. The desktop backend blocks account APIs until a valid signed license lease exists in `~/codex_switch_data/license.json`; the raw license key is never stored locally.

The license authority is a Cloudflare Worker with D1:

```bash
cd /Users/liorhadad/codex_switch
npm run license:generate-signing-key
npx wrangler d1 create codex-switch-license
```

Copy the returned D1 database ID into `wrangler.license.toml`. Put the generated public JWK in `codex_profile_switcher/license_config.py`, and set `DEFAULT_LICENSE_API_BASE` to the deployed Worker URL before building the app.

Set Worker secrets:

```bash
npx wrangler secret put LICENSE_PRIVATE_JWK --config wrangler.license.toml
npx wrangler secret put LICENSE_KEY_PEPPER --config wrangler.license.toml
npx wrangler secret put ADMIN_TOKEN --config wrangler.license.toml
```

Apply the D1 schema and deploy:

```bash
npx wrangler d1 migrations apply codex-switch-license --remote --config wrangler.license.toml
npx wrangler deploy --config wrangler.license.toml
```

Generate manual key batches after deployment:

```bash
CODEX_SWITCH_LICENSE_API_BASE=https://your-worker-url \
CODEX_SWITCH_LICENSE_ADMIN_TOKEN=your-admin-token \
npm run license:admin -- generate --count 25 --notes "launch batch"
```

Support actions are available with the same admin command:

```bash
npm run license:admin -- revoke --key CSW-XXXXX-XXXXX-XXXXX-XXXXX
npm run license:admin -- reset --key CSW-XXXXX-XXXXX-XXXXX-XXXXX
```

Reset frees a key for a legitimate reinstall. Revoke blocks future refreshes; already-running apps stop once they refresh or their 7-day lease expires.

## Backend Only

```bash
cd /Users/liorhadad/codex_switch
python3 /Users/liorhadad/codex_switch/main.py
```

## Tests

```bash
cd /Users/liorhadad/codex_switch
python3 -m unittest discover -s tests -v
npm run test:license-worker
npm run web:build
```
