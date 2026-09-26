# Prospective GMX Arbitrum G5 protocol — frozen v1

Author: Phirawit Jitnarong, independent researcher. Prepared 2026-09-26 UTC.
The external deposit URL, server timestamp, immutable revision/commit ID,
and this file's SHA-256 belong in a separate receipt. This text does not
assert that deposit has already occurred.

## Scope and prior information

This is a **new GMX Synthetics Arbitrum arm**, not an amendment or backdating
of the locked Hyperliquid Part 2/3 registration. The Hyperliquid G5 remains
open. The GMX Sep 19–26 census, its 100% rounded source-internal coverage,
21.0749% seven-day trade-discovery gap, seven on-chain position-key checks,
and the Sep 26 one-day on-chain OI check were all known when this protocol
was written. They are exploratory design information, not holdout results.
The one-raw-unit OI tolerance below is informed by that disclosed pilot.
No future-window outcomes have been used to set these rules.

## Fixed window and universe

The cold-start/event interval is [2026-09-27 00:00:00,
2026-10-04 00:00:00) UTC. The Sep 27 00:00 UTC position/OI state is the
baseline. The seven **primary coverage** states are exactly 00:00:00 UTC on
Sep 28, Sep 29, Sep 30, Oct 1, Oct 2, Oct 3, and Oct 4, 2026. A delayed
retrieval must still select these exact `snapshotTimestamp` values; it may
not select a later live state or silently change the interval. The outcome
is not complete before the Oct 4 end state and all event rows are available.

For each scheduled state, the primary market universe is **all** GMX
Synthetics Arbitrum `FundingBalanceOiSnapshot` rows at that timestamp. Do
not preselect coins, accounts, or position sizes. Report zero-OI markets and
all positive-OI market-time cells. The independent on-chain `MARKET_LIST`
check also reports chain-only markets; a chain-only market with positive
on-chain OI makes that day's independently validated result inconclusive,
not zero-filled. A chain-only zero-OI market is reported but excluded from
the positive-OI denominator.

## Sources, matching, and frozen calculations

The **primary** source is GMX's public Arbitrum Subsquid GraphQL endpoint
`https://gmx.squids.live/gmx-synthetics-arbitrum:prod/api/graphql`, as
documented at `https://docs.gmx.io/docs/api/graphql/`. Fetch every
`Position` with `isSnapshot=true`, exact `snapshotTimestamp`, and
`sizeInUsd>0` through the full `positionsConnection` cursor. Fetch every
`FundingBalanceOiSnapshot` at that timestamp through the full corresponding
connection. Fetch all `TradeAction` rows with non-null account and timestamp
in the fixed half-open event interval through the full connection. Compare
each connection's returned count to `totalCount`; require unique position
IDs and market addresses, non-null accounts, exact timestamps, and complete
pagination. Record HTTP failures, retries, request times, and page counts;
never silently discard a failed or partial day.

All USD position and OI fields are integer units of 10^-30 USD. For a
positive-OI market *m* at time *t*, compute

`C(m,t) = sum(sizeInUsd of long and short open positions in m,t) /
          (longOpenInterestUsd(m,t) + shortOpenInterestUsd(m,t))`.

Compute separate long and short ratios when that side's OI is positive.
For each scheduled day, venue-wide weighted coverage is the sum of all
positive-market position sizes divided by the corresponding sum of both
OI sides. The primary coverage statistic is the **median of the seven
daily venue-wide ratios**. Report its minimum, each daily ratio, the median
and minimum across positive-OI market-time cells, and side-specific median
and minimum. Do not cap ratios at 1 or round raw units before calculation.
Report unmatched positive-size position markets; if one exists, the daily
join fails rather than discarding it. The project-defined numerical G5
coverage threshold is median >=0.70; median <0.50 triggers Pivot A;
[0.50,0.70) is not a pass. No separate minimum-ratio threshold is invented.
If any one of the seven daily states fails primary completeness/join checks,
classify the seven-day primary **inconclusive**, even if the available-day
median would pass.

The cold-start numerator is the sum of end-state position `sizeInUsd`
where `openedAt < 2026-09-27 00:00:00 UTC` and the position's account
does **not** appear in any `TradeAction.account` in the fixed event interval.
Divide by the Oct 4 end-state two-sided venue OI. Report cold position count,
cold distinct-account count, total event rows and distinct event accounts,
end position rows/accounts, and `1 - cold_start_gap` as trade-discovery
coverage. This is a **discovery-path gap**, not missingness of the full
position census, liquidation accuracy, or a trading forecast. If event
pagination or the end state is incomplete, cold-start is inconclusive.

Time-to-stable (primary-source) is the first of the seven scheduled coverage
states for which complete cursors, timestamps, unique keys, OI join, and
coverage calculation pass. Also report the first independently validated
state under the checks below, or `not achieved`. A later successful day does
not rescue a missing primary day for the seven-day median.

## Independent on-chain checks (separate from the primary source)

At each scheduled block recorded in the OI snapshot, independently read
Arbitrum chain ID, block hash/time, GMX DataStore `POSITION_LIST` count and
all keys, `MARKET_LIST` addresses, each market's long/short `OPEN_INTEREST`
storage, and a deterministic seven-key quantile sample of `SIZE_IN_USD`.
Use the contract addresses and storage-key construction in GMX's published
contracts (`https://docs.gmx.io/docs/api/contracts/addresses/` and
`https://github.com/gmx-io/gmx-synthetics`). Compare full key sets, not just
counts; record only set digests and mismatch counts. Require the OI snapshot
rows to share one block number whose block timestamp equals the scheduled
timestamp. `Position` rows expose no block number: a matching key set at the
OI block supports, but does not prove, every non-key field's exact-block
identity. State that limitation explicitly.

For an **independently validated day**, require zero symmetric difference
between chain and indexer position-key sets; zero positive-OI chain-only
markets; each indexed market-side OI to differ from same-block on-chain OI
by at most **one raw 10^-30 USD unit**; and all seven sampled individual
sizes to match exactly. Report exact-match counts and raw differences,
including any one-unit differences. A failure or inaccessible historical
RPC makes that day's independent check inconclusive; the GraphQL-only result
may still be reported as source-dependent, but never as independently
validated. The sampled-size check does not certify every position value.
Use no paid RPC, AWS requester-pays service, or undisclosed API credentials.
The public Arbitrum RPC can be tried near the snapshot; any fallback public
RPC used for secondary verification must be named in the receipt. Do **not**
switch the frozen primary GraphQL endpoint or substitute a different OI
denominator after seeing outcomes. Provider deprecation or rate limits are
failure states, not a reason to relax the check.

## Collection, retention, and status language

Automated collection may run after a scheduled state is published, including
later retries, because the query pins the exact UTC timestamp. Attempt the
baseline and each primary day promptly; retry incomplete fixed-time days
without changing the selected time. Record first successful retrieval time,
all failed attempts, GraphQL and RPC endpoints, HTTP status, counts, block
metadata, code version, and aggregate checksums. A missed automation run
is not itself proof of a missed *snapshot*, but a day remains incomplete
until the pinned historical rows are actually retrieved and verified.
Persist no wallet-address rows, account lists, raw full responses, or API
secrets in a public repository. Keep account identifiers only in process
memory for the final event-set join. Publish protocol, code, failure/success
receipts, and non-identifying aggregates. Manually review live provider
terms and venue/publication rules before using the derived results in a
submission; this protocol does not assert a redistribution licence.

Classify the outcome on two axes: (1) primary seven-day GraphQL numerical
gate = pass, not-pass, or inconclusive; and (2) independent-chain validation
= complete, partial, or unavailable. Never call the original Hyperliquid G5
closed because this GMX arm passes. Never call any observation a completed
prospective holdout before Oct 4 and the final full-window calculation.
Distinguish a public timestamped GitHub deposit from formal OSF-style
preregistration; give the exact URL, revision ID, SHA-256, and server
timestamp in a separate receipt. The deposit must precede the **Sep 27
00:00 UTC baseline**, not merely the Sep 28 first coverage snapshot.
