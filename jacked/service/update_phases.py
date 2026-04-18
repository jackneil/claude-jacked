"""Single source of truth for update phases.

Consumed by:
  - jacked.service.updater.run_update() (POSIX updater)
  - jacked.service.updater._spawn_windows_tray_updater() (Windows batch generator)
  - jacked.cli._spawn_windows_upgrade_helper() (Windows batch generator)
  - jacked/data/web/update.html (embedded copy — test_update_html_embeds_all_phase_names
    enforces consistency)

If you add or rename a phase here, the test_update_html_embeds_all_phase_names
test will fail until you update update.html to match.
"""

PHASES: list[dict] = [
    {"name": "waiting_for_parent", "label": "Waiting for old tray to exit"},
    {"name": "installing_package", "label": "Installing package"},
    {"name": "migrating_settings", "label": "Migrating settings"},
    {"name": "waiting_port_free",  "label": "Waiting for port to free"},
    {"name": "starting_service",   "label": "Starting new service"},
    {"name": "verifying_service",  "label": "Verifying new service"},
]

PHASE_NAMES: list[str] = [p["name"] for p in PHASES]
