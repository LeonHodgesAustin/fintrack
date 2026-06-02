"""
fintrack.assets — manual asset / liability tracking.

Covers three asset types that cannot be pulled automatically from Plaid:
  loans    — mortgage and auto loans (amortization + balance)
  vehicles — owned vehicles (declining-balance depreciation)
  equity   — RSU grants and ESPP plans (vesting math + live stock prices)

All types feed into `net_worth.snapshot()` for a single net-worth number.
"""
