# Lessons

- [1x] PyPI publishing is handled via GitHub Actions trusted publishing. NEVER use twine to upload directly. Create a GitHub release and PyPI pulls it automatically.
- [1x] This project uses the `jacked` conda env (C:/Users/jack/.conda/envs/jacked/python.exe), NOT krac_llm.
- [1x] When adding shell operators to SHELL_OPERATOR_RE, enumerate ALL operators including redirection (>, >>, <) and newlines (\n). Missing even one creates a bypass where safe-prefix commands can write to arbitrary files.
- [1x] SAFE_PREFIXES entries that also exist in SAFE_EXACT must have a trailing space (e.g. "env " not "env") — otherwise startswith() matches unintended commands like envsubst, lsblk, etc.
- [1x] After changing install/uninstall logic, ALWAYS verify the live hook path in ~/.claude/settings.json points to the right python and script. Stale pipx/pip copies mean your code changes are invisible.
- [1x] When designing LLM prompts for the gatekeeper, ALWAYS require a "reason" field in both allow AND deny JSON responses. Without it, logs are useless for auditing decisions.
- [1x] Avoid double-logging: raw LLM response goes to log_debug() only, parsed DECISION line goes to log(). Users only see the main log — put all useful info (method, reason, timing) on the DECISION line.
