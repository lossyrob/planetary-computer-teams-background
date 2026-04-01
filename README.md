# Generate Teams backgrounds from the Planetary Computer

This repository contains a script that rewrites a single custom Microsoft Teams background with fresh Sentinel-2 imagery from the Microsoft Planetary Computer.

The script does **not** call a Teams API. It works by updating image files in Teams' local `Backgrounds\Uploads` folder, so you select the generated background once in Teams and later runs replace the image contents in place.

If Teams copies your chosen background to a GUID-named file in `Backgrounds\Uploads`, point `settings.yaml:image_name` at that GUID file so future refreshes update the background Teams is actually using.

## Windows quick start

Set it up as a managed background task:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
.\scripts\install-windows-task.ps1 -PythonExe .\.venv\Scripts\python.exe -SettingsFile .\settings.yaml -StartNow
```

The installer automatically switches to `pythonw.exe` when it is available, so the scheduled task runs without opening a terminal window. Pass `-ConsoleWindow` only if you explicitly want a visible console.

Manage it with:

```powershell
Get-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Start-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Stop-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Disable-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Enable-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Get-Content ".\logs\runner.log" -Wait
```

Remove it with:

```powershell
.\scripts\uninstall-windows-task.ps1
```

## Requirements

- Python 3.10+
- Windows desktop Teams (new or classic)
- Optional: a GeoJSON FeatureCollection of AOIs

## Setup

Create and activate a virtual environment, then install the dependencies.

### PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Review and edit `settings.yaml` as needed for your machine. `settings.template.yaml` remains a reference copy.

## Running

```powershell
python pc_teams_background.py
```

Useful flags:

- `-f`, `--force`: regenerate immediately
- `-d`, `--debug`: raise the full exception instead of printing a short error
- `--settings-file`: point at a different settings YAML file

The script generates a new background when any of these are true:

- the configured background file does not exist
- Teams has read the current background file since it was generated
- `force_regen_after` has elapsed
- the current background came from an AOI and `aois.refresh_days` has elapsed

When a new image is needed, the script:

1. Searches recent Sentinel-2 items from the configured collections.
2. Prefers new items intersecting configured AOIs.
3. Falls back to a random recent item if no AOI match is available.
4. Crops the selected item to the configured background aspect ratio.
5. Writes the background image, a Teams thumbnail, and an info JSON file.

## Teams background folder

If `teams_image_folder` is left blank, the script auto-detects the first existing folder from:

- `%LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\LocalCache\Microsoft\MSTeams\Backgrounds\Uploads`
- `%LOCALAPPDATA%\Microsoft\MSTeams\Backgrounds\Uploads`
- `%APPDATA%\Microsoft\Teams\Backgrounds\Uploads`

For a safe dry run, point `teams_image_folder` at a temporary folder first. Once you are happy with the results, switch it back to the real Teams folder and select the generated background in Teams once.

## AOIs

You can provide a GeoJSON FeatureCollection of AOIs to prefer certain places in the rotation. One easy way to create the file is [geojson.io](https://geojson.io).

This repo now includes an `aois.geojson` file that you can point your local `settings.yaml` at directly.

When AOIs are enabled, the script will:

- assign feature IDs if they are missing
- store the last-used image timestamp in each feature's properties
- avoid reusing an AOI too quickly based on `aois.refresh_days`
- fall back to the AOI/item overlap when an AOI is much larger than a single Sentinel scene, which avoids mostly black backgrounds with a tiny image patch

## Windows background mode

For Windows, the best fit is a **per-user scheduled task**, not a true Windows service.

Why:

- Teams backgrounds live in your user profile
- the current Teams background folder is user-specific
- running before you sign in is usually not useful because Teams is not running yet
- scheduled tasks are easy to start, stop, disable, and inspect without dealing with service logon credentials

This repo includes a long-running runner plus install/remove scripts for that setup.

### Runner

Run the background loop directly:

```powershell
python pc_teams_background_runner.py --interval-seconds 900
```

Useful flags:

- `--settings-file`: point at a different settings YAML file
- `--log-file`: choose a log file path
- `--log-level`: set log verbosity
- `--force-first-run`: force the first iteration after startup
- `--once`: run one iteration and exit

By default, the runner logs to:

```text
<repo>\logs\runner.log
```

### Install the scheduled task

With a virtual environment in `.venv` and a local `settings.yaml`:

```powershell
.\scripts\install-windows-task.ps1 -StartNow
```

Or specify everything explicitly:

```powershell
.\scripts\install-windows-task.ps1 `
  -PythonExe .\.venv\Scripts\python.exe `
  -SettingsFile .\settings.yaml `
  -IntervalSeconds 900 `
  -StartNow
```

The task starts automatically when you sign in after a reboot.

### Manage it

```powershell
Get-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Start-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Stop-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Disable-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Enable-ScheduledTask -TaskName "PlanetaryComputerTeamsBackground"
Get-Content ".\logs\runner.log" -Wait
```

Remove it with:

```powershell
.\scripts\uninstall-windows-task.ps1
```

### True Windows service

If you really want a service, host `pc_teams_background_runner.py` with a service wrapper such as NSSM or WinSW **under your own Windows user account**, not `LocalSystem`.

A true service is less attractive here because:

- service logon credentials add setup overhead
- the process still needs access to your user profile and Teams background folder
- Task Scheduler already gives you start/stop/disable/status controls and persistent logs

WSL/cron can still work too if you prefer that setup.

## Contributing

This project welcomes contributions and suggestions.  Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit https://cla.opensource.microsoft.com.

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
