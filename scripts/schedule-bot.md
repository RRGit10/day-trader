# Start the bot automatically at 9:25 AM ET, Monday to Friday

All three examples run `daytrader run --mode bot` from the project folder (the one with `config.toml` and `.env`). Replace `/path/to/project` and use the full path to the `daytrader` executable. Find it with `which daytrader` (Linux/macOS) or `where daytrader` (Windows).

The bot is paper-only. Weekdays that are market holidays are not skipped by the scheduler; the bot simply finds no market data and trades nothing. Check `daytrader status` and `daytrader report --journal bot` now and then.

## Linux: cron

Run `crontab -e` and add:

```
CRON_TZ=America/New_York
25 9 * * 1-5 cd /path/to/project && /full/path/to/daytrader run --mode bot >> state/bot-cron.log 2>&1
```

`CRON_TZ` works in cronie (Fedora, RHEL, Arch) and some other crons. If your cron ignores it (Debian/Ubuntu's default may), convert 9:25 AM ET to your local time and edit the hour and minute. Eastern is UTC-5 in winter and UTC-4 in summer, so the local time shifts when daylight saving changes in the US.

## macOS: launchd

Save as `~/Library/LaunchAgents/com.daytrader.bot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.daytrader.bot</string>
  <key>WorkingDirectory</key><string>/path/to/project</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>/full/path/to/daytrader</string>
    <string>run</string>
    <string>--mode</string>
    <string>bot</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>25</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>25</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>25</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>25</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>25</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/path/to/project/state/bot-launchd.log</string>
  <key>StandardErrorPath</key><string>/path/to/project/state/bot-launchd.log</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/com.daytrader.bot.plist`. launchd uses your Mac's local time, so Hour 9 is only 9:25 ET if your Mac is on Eastern time. Otherwise convert the hour (and the weekday, if the conversion crosses midnight). The Mac must be awake at that time: `caffeinate -i` (above) keeps it awake while the bot runs, and `sudo pmset repeat wakeorpoweron MTWRF 09:15:00` wakes it each weekday at 9:15. Keep it plugged in with the lid open (a closed MacBook sleeps anyway unless an external display is attached).

## Windows: Task Scheduler

In Command Prompt (adjust paths):

```
schtasks /Create /TN "daytrader-bot" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:25 /TR "cmd /c cd /d C:\path\to\project && C:\full\path\to\daytrader.exe run --mode bot >> state\bot-task.log 2>&1"
```

Task Scheduler uses the PC's local time, so `/ST 09:25` is 9:25 ET only if Windows is set to Eastern time. Otherwise convert it. Remove the task with `schtasks /Delete /TN "daytrader-bot"`.
