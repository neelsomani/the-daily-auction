# Solana Auction Program + Nightly Settlement Job

This spec defines the on-chain auction program and the off-chain AWS nightly job.
Assumptions:
- `Clock.unix_timestamp` is trusted.
- Settlement uses UTC midnight boundaries.
- The AWS job runs at real-world UTC midnight and retries for up to 30 minutes.
- Loser fee is 0.0001 SOL (100,000 lamports) paid to the refund cranker.

## Constants

- `seconds_per_day = 86_400`
- `min_increment = 0.1 SOL = 100_000_000 lamports`
- `loser_fee = 0.0001 SOL = 100_000 lamports`
- `max_batch_size = implementation choice (suggest 20 to 30 recipients per tx)`
- `init_day_max_ahead_days = 2`

## Time Model

- `day_index = floor(clock.unix_timestamp / 86_400)`
- The auction "day" is exactly this `day_index`.
- Settlement for day `D` is allowed when `current_day_index >= D + 1`.

### Midnight Buffer

On-chain rule (strict): settlement only after midnight UTC per on-chain clock.
Off-chain behavior (buffer via retries): AWS retries for up to 30 minutes after real-world midnight until the on-chain condition is true.

## On-Chain Accounts

### Config PDA

Seed: `["config"]`

Fields:
- `recipient_pubkey: Pubkey` (neelsalami.sol)
- `loser_fee_lamports: u64` (100_000)
- `min_increment_lamports: u64` (100_000_000)

Initialized once.

### AuctionDay PDA

Seed: `["auction_day", day_index_le_bytes]`

Fields:
- `day_index: i64`
- `finalized: bool`
- `winner: Pubkey` (default `Pubkey::default()` if no bids)
- `highest_bid: u64`
- `bidder_count: u32`
- `refund_count_total: u32` (optional, for operational tracking)
- `refund_count_completed: u32` (optional, for operational tracking)
- `total_bid_lamports: u64`
- `refund_pool_remaining: u64`
- `fee_pool_remaining: u64`
- `vault_bump: u8`

### Vault PDA

Seed: `["vault", auction_day_pubkey]`

System account PDA holding lamports for the day. It must be created (zero data, lamports only).
In Anchor this is typically `SystemAccount` with `init_if_needed` and `space = 0` (or minimal).

### BidReceipt PDA (per bidder per day)

Seed: `["bid_receipt", auction_day_pubkey, bidder_pubkey]`

Fields:
- `auction_day: Pubkey`
- `bidder: Pubkey`
- `amount: u64`
- `refunded: bool`

## Instructions

### A) `init_day(day_index)` (optional)

Purpose:
Optionally pre-create the `AuctionDay` and `Vault` for a given `day_index`.

Requirements:
- Callable by anyone.
- Idempotent. If the account already exists, it is a no-op (or returns a clear "already initialized" error).
- The system does not depend on this instruction because `place_bid` uses `init_if_needed`.
- Require `day_index <= current_day_index + init_day_max_ahead_days` to prevent rent griefing.

### B) `place_bid(day_index, new_amount)`

Purpose:
Place or raise a bid for a day.

Requirements:
- Enforce the current day by on-chain clock:
  - Compute `current_day_index = floor(clock.unix_timestamp / 86_400)`.
  - Require `day_index == current_day_index`.
- `AuctionDay` and `Vault` must be created if missing:
  - `AuctionDay`: `init_if_needed`, payer = bidder.
  - `Vault`: `init_if_needed`, payer = bidder (system account PDA).
- On first creation, initialize:
  - `day_index = current_day_index`
  - `finalized = false`
  - `winner = Pubkey::default()`
  - `highest_bid = 0`
  - `bidder_count = 0`
  - `total_bid_lamports = 0`
  - `refund_pool_remaining = 0`
  - `fee_pool_remaining = 0`
  - `vault_bump` set appropriately
- Require `AuctionDay.finalized == false`.
- Enforce increment rule:
  - If `highest_bid == 0`, require `new_amount >= min_increment`.
  - Else require `new_amount >= highest_bid + min_increment`.
- Per bidder, store only their latest bid amount in `BidReceipt`.
- On bid increase, transfer only the delta from bidder to vault.
- Updates:
  - `bid_receipt.amount = new_amount`
  - `auction_day.total_bid_lamports += delta`
  - If `new_amount > highest_bid`, set `highest_bid` and `winner`
  - If `BidReceipt` is newly created in this call, increment `bidder_count` by 1; otherwise do not change `bidder_count`

### C) `settle_day(day_index)`

Purpose:
Finalize the auction for a day, compute pools, and pay the recipient.

Requirements:
- Require `AuctionDay.finalized == false`.
- Compute `current_day_index = floor(clock.unix_timestamp / 86_400)` inside `settle_day`.
- Require `AuctionDay.day_index == day_index`.
- Require `day_index < current_day_index` (same as `current_day_index >= day_index + 1`).
- If `highest_bid == 0` (treat as "no bids"):
  - Set `finalized = true`
  - `winner` stays default
  - No transfers
  - `refund_pool_remaining = 0`
  - `fee_pool_remaining = 0`
  - Return
- Else:
  - `loser_count = bidder_count - 1`
  - `loser_sum = total_bid_lamports - highest_bid`
  - `fee_pool = loser_count * loser_fee`
  - Require `loser_sum >= fee_pool` (should hold if bids are large enough)
  - `refund_pool = loser_sum - fee_pool`
- Transfers at settlement:
  - If vault lamports are insufficient for `total_bid_lamports`, error
  - Pay `recipient_pubkey` exactly `highest_bid` lamports
  - Do not pay `fee_pool` to recipient (reserved for the refund cranker)
- Set:
  - `refund_pool_remaining = refund_pool`
  - `fee_pool_remaining = fee_pool`
  - `finalized = true`
  - Optional: `refund_count_total = bidder_count - 1`, `refund_count_completed = 0`

### D) `refund_batch(day_index, bidders[])`

Purpose:
Pay refunds to losing bidders and the per-refund cranker fee.

Requirements:
- Require `AuctionDay.finalized == true`.
- Caller must be a signer (anyone can call).
- For each bidder in `bidders[]`:
  - Load `BidReceipt` for `(auction_day, bidder)`
  - If `refunded == true`, continue
  - If `bidder == winner`, mark `bid_receipt.refunded = true` and continue (no transfers)
  - Require `bid_receipt.amount > loser_fee`
  - `refund_amount = bid_receipt.amount - loser_fee`
  - If `refund_pool_remaining < refund_amount` or `fee_pool_remaining < loser_fee`, error
  - If vault lamports are insufficient for `refund_amount + loser_fee`, error
  - Transfer `refund_amount` from vault to bidder
  - Transfer `loser_fee` from vault to caller (cranker)
  - Mark `bid_receipt.refunded = true`
  - Decrement `refund_pool_remaining` and `fee_pool_remaining`
  - Optional: increment `refund_count_completed` when a receipt is marked refunded (including winner)
- Must be safe to call multiple times and safe to retry the same batch.
- Batch size bounded by transaction limits, so the instruction must support partial completion and repeated calls.

## Query Requirements

Anyone can query today’s winner off-chain:
- Compute `today_day_index` from `Clock` off-chain
- Derive `AuctionDay` PDA for `today_day_index`
- Read `winner` and `highest_bid`
- If account missing, treat as “no bids yet”

Time until next winner is decided (off-chain):
- `now = clock.unix_timestamp`
- `current_day_index = floor(now / 86_400)`
- `next_midnight = (current_day_index + 1) * 86_400`
- `seconds_remaining = next_midnight - now`

Optional: expose a read-only helper instruction, but not required.

## Economic Requirements

- Losers receive `final_bid_amount - 0.0001 SOL`.
- The 0.0001 SOL per losing bidder is paid to the refund cranker (transaction signer).
- Funds flow:
  - Winner’s bid goes to `recipient_pubkey`
  - Loser fees go to the refund cranker proportional to refunds processed
  - Losers receive refunds net of the fee
- No one can withdraw from the vault except via `settle_day` and `refund_batch`.

## Off-Chain Nightly Job (AWS)

### Schedule

Trigger at `00:00:00 UTC` daily.

### Phase 1: Settle Yesterday

- `target_day_index = floor(real_utc_now / 86_400) - 1`
- Call `settle_day(target_day_index)`
- If it fails with `TooEarly` (on-chain clock behind), retry every 30 to 60 seconds for up to 30 minutes
- If it fails for transient reasons, retry with backoff
- If already finalized, proceed

### Phase 2: Refund Losers in Batches

- Fetch all `BidReceipt` accounts for `auction_day` `target_day_index`
- Build loser list: receipts where `bidder != winner` and `refunded == false`
- Chunk losers into batches of `max_batch_size`
- For each batch:
  - Call `refund_batch(target_day_index, batch_bidders)`
  - Retry failed batches
- Stop when all losers are refunded or when a configured max runtime is hit (resume next run)

### Observability

Log:
- Winner and `highest_bid`
- Number of bidders, number of losers refunded
- Total lamports paid to recipient
- Total lamports paid as cranker fees
- Remaining pools on-chain (`refund_pool_remaining`, `fee_pool_remaining`)

## Edge Cases

- No bids: `settle_day` finalizes with no transfers.
- Only one bidder: winner chosen, no refunds, no fee pool.
- Bidders can increase their own bid (only delta transferred).
- Bids cannot decrease.
- `refund_batch` can be run by anyone for liveness.
  - If you want to restrict it, require `caller == crank_authority` in config, but refunds can stall if the job dies.

## Rent / Account Creation

- The first bidder for a day pays rent for `AuctionDay`, `Vault`, and their `BidReceipt`.
- Subsequent bidders pay rent only for their `BidReceipt` when it is first created.

## Environment Variables (AWS Job)

- `CRANKER_PRIVATE_KEY` (new) used to sign `refund_batch` calls.
