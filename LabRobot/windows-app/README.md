# Lab Robot Windows App

Native Windows WebView2 workspace for Claude Studio, Google Veo, Microsoft Copilot, and Lab Robot Control.

## Install

Run from PowerShell:

```powershell
.\Install-LabRobotWindowsApp.ps1
```

This publishes the .NET 8 application to the current user's local app-data directory and registers the `stratiq-labrobot://` URI protocol. The Launch Modules page uses that protocol for the Lab Robot tile.

The Microsoft Edge WebView2 Evergreen Runtime must be installed. Windows 11 and current Microsoft Edge installations normally include it.

## Configuration

The following environment variables can override workspace destinations:

- `LAB_ROBOT_WEB_URL`
- `LAB_ROBOT_CLAUDE_URL`
- `LAB_ROBOT_VEO_URL`
- `LAB_ROBOT_COPILOT_URL`

To force the portal tile back to browser-only behavior, set `REACT_APP_LAB_ROBOT_DESKTOP_ENABLED=false` when building the App Rationalization frontend.
