# Demo fixture

`demo-data` impersonates `logibar-status` for README screenshots: a keyboard at 92% **charging**, a mouse at 38%, and a headset at 8% in the **critical** band — so one shot shows the charging glyph, the healthy/mid/critical spread across the battery ramp, and the urgent treatment that no real device is usually doing. Timestamps are generated at run time, staggered a few minutes apart, so the "Updated HH:MM" lines always look fresh.

It writes demo state files to a temp dir and runs the real `logibar-status` against them, so `--json`, the Waybar module, `--devices`, `--frame`, `--no-color` and `NO_COLOR` all behave exactly as in production.

```bash
PATH="$PWD/screenshots/demo:$PATH" logibar-status --json   # or waybar mode, or the Omarchy plugin
./screenshots/demo/demo-data --json                        # call the fixture directly
```

`logibar-status` in this directory is a one-line shim onto `demo-data`, so putting the directory first on PATH is enough for any frontend to pick the fixture up.

Documentation tooling only — it is not installed, not wired into `make`, and not part of the test suite.
