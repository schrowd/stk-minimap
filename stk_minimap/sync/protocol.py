"""
Tuning constants for talking to a patched SuperTuxKart's --sync-port
listener. See patches/PROTOCOL.md for the full wire contract.
"""

# A STATE within this many seconds of the local head is treated as "already
# agrees" and dropped - the recorded frame interval, per patches/PROTOCOL.md,
# so this isn't chasing noise finer than the data.
SYNC_DEADBAND = 0.1

# A local control wins over an inbound STATE for this long afterwards, so a
# message already in flight when the user acts can't immediately overwrite
# what they just did.
SYNC_LOCAL_HOLDOFF = 0.25

SYNC_DEFAULT_PORT = 27982
