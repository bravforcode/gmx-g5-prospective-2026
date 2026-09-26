# GMX G5 prospective collection record

This repository runs the [frozen public protocol](https://gist.github.com/bravforcode/d139701fb92b4dad22e89756e9e14fa8)
deposited at 2026-09-26T18:08:00Z, before the 2026-09-27T00:00:00Z
baseline. The GitHub Gist first revision is
`b088b7cc5ef279237de5f75fdb68bcd07cd6dd1e`; the protocol SHA-256 is
`efa241ab8bb57be1ae582c15a0bdef4932e14005613e3dd5375c96892e95fee0`.
The historical Hyperliquid registration is a different study arm and is not
closed by this collection.

The workflow attempts fixed-time snapshots four times daily at 00:25,
06:25, 12:25 and 18:25 UTC. GitHub Actions cron delivery can be delayed or
skipped; the script always queries the *specified* historical UTC snapshot,
not the runner's execution time, and retries missing receipts on later runs.
The baseline is Sep 27; seven coverage snapshots are Sep 28–Oct 4 at 00:00
UTC. The full event/cold-start calculation begins only after Oct 4 00:00 UTC.
Running the optional `pilot` dispatch writes under `receipts/pilot/` and is
not a future-window observation.

`receipts/YYYY-MM-DD.json` contains only aggregate counts/ratios, hashes,
request metadata, and chain-audit output. It contains no wallet addresses or
raw position rows. `results/g5_final.json` is not a valid completed result
unless it explicitly reports eight complete fixed-time receipts and a final
event join; its two status axes separate the numerical gate from independent
chain validation. A failed or unavailable chain check remains visible and
cannot be promoted into a successful one by the source-internal OI ratio.

This public record is not a guarantee of uptime, free-RPC continuity,
complete data, or provider publication rights. Do not represent a scheduled
job as a completed holdout before checking the actual committed receipts and
terms. Disable the recurring workflow after the final audit or the Oct 11
stop date to avoid indefinite no-op runs.
