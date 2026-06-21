# Backupman

Small GRCLib-based backup tool for server file-browser backups.

## Requirements

- Python 3.10+
- GRCLib shared library from the latest release:
  - Windows: `grclib.dll`
  - Linux: `grclib.so`
  - macOS: `grclib.dylib`

Put the GRCLib library beside `_backupman.py`, or set `grclib_path` in `config.json`.

## Install

Windows:

```powershell
py -m pip install windows-curses
```

Linux/macOS usually include `curses` with Python, so no pip package is normally needed.

## Configure

Copy `config.example.json` to `config.json`, then fill in your listserver/account settings.

Useful config fields:

- `grclib_path`: optional full path to the GRCLib library.
- `force_rescan`: ignore previous file index and scan again.
- `only_download_enabled`: only download folders listed in `only_download_folders`.
- `skip_folders`: folders to ignore.
- `backupboi_compat_pcid`: keeps Backupman using its old trusted PCID identity through GRCLib.

## Run

```powershell
py _backupman.py
```

Backups are written under `Servers/<server name>/`.
