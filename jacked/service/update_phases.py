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
    {"name": "preflight", "label": "Checking the new version can start"},
    {"name": "migrating_settings", "label": "Migrating settings"},
    {"name": "waiting_port_free", "label": "Waiting for port to free"},
    {"name": "starting_service", "label": "Starting new service"},
    {"name": "verifying_service", "label": "Verifying new service"},
    # After the service is back: downloading skill packs no longer keeps the
    # service down (#178i). A failure here is a "warning": the upgrade itself
    # succeeded, and `jacked packs update` retries it.
    {"name": "refreshing_packs", "label": "Refreshing skill packs", "may_warn": True},
    {"name": "rolling_back", "label": "Rolling back to the previous version"},
]

PHASE_NAMES: list[str] = [p["name"] for p in PHASES]

# Phases that can end as "warning" without failing the update. update.html
# must explain each one in its done banner (PHASE_WARNINGS there).
WARNING_PHASE_NAMES: list[str] = [p["name"] for p in PHASES if p.get("may_warn")]
