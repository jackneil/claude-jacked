"""The window keeper: it pings each account so that its usage windows open on time.

The full-sweep loop runs the keeper (:mod:`.sweep`). :mod:`.state` holds the
ping backoff and the sweep wake Event, :mod:`.auth_ladder` the candidate
tokens and the 401/403 recovery, :mod:`.confirm` the success path, and
:mod:`.process` the work for one account. :mod:`jacked.api.usage_monitor`
re-exports the names that callers import from it, and
:mod:`jacked.api.keeper_confirmation` settles a pending ping.
"""
