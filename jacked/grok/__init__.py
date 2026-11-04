"""xAI Grok provider support for jacked.

Grok keeps one login in ``~/.grok/auth.json`` and refreshes that file itself.
jacked stores identity plus a per-account copy of the file. It does not store
or exchange the refresh token. Switching writes a stored copy back only after
capturing the live file, and it refuses to replay an older copy of the same
login over a newer one.
"""
