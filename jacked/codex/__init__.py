"""OpenAI Codex provider support for jacked.

jacked treats Codex as a first-class second provider alongside Claude. This
package holds everything Codex-specific (credential reading/identity, usage,
account switching) so the Claude paths stay untouched. See
docs/design/2026-06-28-codex-integration.html for the grounding.
"""
