# EnderCR10SE (enders3dp:7127): retired

This was **not a separate printer**. It was the Ender 3 S1 Plus's Klipper instance on enders3dp, before the S1 Plus
moved to its own host. It is now **enders1plus** on 3dpnas:7125. Evidence: bed 316×304×300 (the same as
enders1plus), Mainsail named it "S1 Plus", and its jobs were Jan 2025 S1 Plus calibration runs.

Retired 2026-10-23:
- **Nothing was migrated to 3dpnas.** All 15 history jobs and the one G-code not already on 3dpnas were calibration
  runs, which Ironman called noise. 34 of its 35 G-code files were already on 3dpnas.
- Its configs (origin = farm snapshot 17) and the host's own history are kept here.
- Removed from `printers/hosts.json`; staged changes 13/14 cancelled.
- Its services were removed from enders3dp with KIAUH: klipper, moonraker, moonraker-obico, moonraker-telegram-bot and
  octoeverywhere, all `-EnderCR10SE`.
