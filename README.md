# Codex Switch

This project now runs as a small Electron shell with a Python backend and a React frontend that mirrors the `flutty_orc` account inspector more closely.

It does:

- import account metadata from `~/flutty_orc_data/codex_relay.db` into `~/codex_switch_data/accounts.json`
- start new local accounts with the same pending browser sign-in flow pattern used by `flutty_orc`
- persist newly added accounts into `~/codex_switch_data/accounts.json` only after the ChatGPT sign-in completes
- create isolated account homes directly under `~/codex_switch_data/prepared_profiles/<account-id>/home`
- render the UI from `~/codex_switch_data`
- persist its own primary-account selection in `~/codex_switch_data/config.json`
- copy the selected account's `.codex/auth.json` into the main Codex home at `~/.codex/auth.json` when Set Primary is clicked
- merge live account rate-limit usage from a running `flutty_orc` instance when available
- map accounts to prepared isolated Codex user-data directories under `~/codex_switch_data/prepared_profiles`
- auto-mark newly connected local accounts as primary in the switcher
- launch Codex with `--user-data-dir=<prepared dir>`

It does not:

- copy browser cookies, local storage, or Keychain items
- write into the default Codex Electron user-data profile under `~/Library/Application Support/Codex`

## Install and Run

```bash
cd /Users/liorhadad/codex_switch
npm install
npm run electron
```

## Backend Only

```bash
cd /Users/liorhadad/codex_switch
python3 /Users/liorhadad/codex_switch/main.py
```

## Tests

```bash
cd /Users/liorhadad/codex_switch
python3 -m unittest discover -s tests -v
npm run web:build
```
