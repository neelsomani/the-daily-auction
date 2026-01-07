use anchor_lang::prelude::*;
use anchor_lang::solana_program::system_program;
use anchor_lang::system_program::{transfer, Transfer};

declare_id!("DtLQpjotSmrKAqk6Sqn16P6dSfKuiXawEyUEgmSmioW6");

const SECONDS_PER_DAY: i64 = 86_400;
const INIT_DAY_MAX_AHEAD_DAYS: i64 = 2;

#[program]
pub mod auction {
    use super::*;

    pub fn init_config(
        ctx: Context<InitConfig>,
        recipient_pubkey: Pubkey,
        loser_fee_lamports: u64,
        min_increment_lamports: u64,
    ) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.recipient_pubkey = recipient_pubkey;
        config.loser_fee_lamports = loser_fee_lamports;
        config.min_increment_lamports = min_increment_lamports;
        config.bump = ctx.bumps.config;
        Ok(())
    }

    pub fn init_day(ctx: Context<InitDay>, day_index: i64) -> Result<()> {
        let current_day_index = current_day_index(&Clock::get()?);
        require!(
            day_index <= current_day_index.saturating_add(INIT_DAY_MAX_AHEAD_DAYS),
            ErrorCode::DayTooFarAhead
        );

        let auction_day = &mut ctx.accounts.auction_day;
        if is_uninitialized_auction_day(auction_day) {
            auction_day.day_index = day_index;
            auction_day.finalized = false;
            auction_day.winner = Pubkey::default();
            auction_day.highest_bid = 0;
            auction_day.bidder_count = 0;
            auction_day.refund_count_total = 0;
            auction_day.refund_count_completed = 0;
            auction_day.total_bid_lamports = 0;
            auction_day.refund_pool_remaining = 0;
            auction_day.fee_pool_remaining = 0;
            auction_day.vault_bump = ctx.bumps.vault;
        }

        require!(
            ctx.accounts.vault.owner == &system_program::ID,
            ErrorCode::InvalidVaultOwner
        );

        Ok(())
    }

    pub fn place_bid(ctx: Context<PlaceBid>, day_index: i64, new_amount: u64) -> Result<()> {
        require!(new_amount > 0, ErrorCode::InvalidBidAmount);

        let clock = Clock::get()?;
        let current_day_index = current_day_index(&clock);
        require!(day_index == current_day_index, ErrorCode::WrongDay);

        let auction_day = &mut ctx.accounts.auction_day;
        if is_uninitialized_auction_day(auction_day) {
            auction_day.day_index = current_day_index;
            auction_day.finalized = false;
            auction_day.winner = Pubkey::default();
            auction_day.highest_bid = 0;
            auction_day.bidder_count = 0;
            auction_day.refund_count_total = 0;
            auction_day.refund_count_completed = 0;
            auction_day.total_bid_lamports = 0;
            auction_day.refund_pool_remaining = 0;
            auction_day.fee_pool_remaining = 0;
            auction_day.vault_bump = ctx.bumps.vault;
        }

        require!(
            ctx.accounts.vault.owner == &system_program::ID,
            ErrorCode::InvalidVaultOwner
        );

        require!(!auction_day.finalized, ErrorCode::AlreadyFinalized);

        let highest_bid = auction_day.highest_bid;
        let min_increment = ctx.accounts.config.min_increment_lamports;
        if highest_bid == 0 {
            require!(new_amount >= min_increment, ErrorCode::BidTooLow);
        } else {
            let required = highest_bid
                .checked_add(min_increment)
                .ok_or(ErrorCode::MathOverflow)?;
            require!(new_amount >= required, ErrorCode::BidTooLow);
        }

        let bid_receipt = &mut ctx.accounts.bid_receipt;
        let is_new_receipt = bid_receipt.bidder == Pubkey::default();
        if is_new_receipt {
            bid_receipt.auction_day = auction_day.key();
            bid_receipt.bidder = ctx.accounts.bidder.key();
            bid_receipt.refunded = false;
            auction_day.bidder_count = auction_day
                .bidder_count
                .checked_add(1)
                .ok_or(ErrorCode::MathOverflow)?;
        }

        require!(
            bid_receipt.bidder == ctx.accounts.bidder.key(),
            ErrorCode::BidderMismatch
        );

        let previous_amount = bid_receipt.amount;
        require!(new_amount > previous_amount, ErrorCode::BidDecrease);
        let delta = new_amount
            .checked_sub(previous_amount)
            .ok_or(ErrorCode::MathOverflow)?;

        if delta > 0 {
            let cpi_ctx = CpiContext::new(
                ctx.accounts.system_program.to_account_info(),
                Transfer {
                    from: ctx.accounts.bidder.to_account_info(),
                    to: ctx.accounts.vault.to_account_info(),
                },
            );
            transfer(cpi_ctx, delta)?;
        }

        bid_receipt.amount = new_amount;
        auction_day.total_bid_lamports = auction_day
            .total_bid_lamports
            .checked_add(delta)
            .ok_or(ErrorCode::MathOverflow)?;

        if new_amount > auction_day.highest_bid {
            auction_day.highest_bid = new_amount;
            auction_day.winner = ctx.accounts.bidder.key();
        }

        Ok(())
    }

    pub fn settle_day(ctx: Context<SettleDay>, day_index: i64) -> Result<()> {
        let current_day_index = current_day_index(&Clock::get()?);
        let auction_day = &mut ctx.accounts.auction_day;

        require!(!auction_day.finalized, ErrorCode::AlreadyFinalized);
        require!(auction_day.day_index == day_index, ErrorCode::DayMismatch);
        require!(day_index < current_day_index, ErrorCode::TooEarly);
        require!(
            ctx.accounts.vault.owner == &system_program::ID,
            ErrorCode::InvalidVaultOwner
        );

        if auction_day.highest_bid == 0 {
            auction_day.finalized = true;
            auction_day.refund_pool_remaining = 0;
            auction_day.fee_pool_remaining = 0;
            auction_day.refund_count_total = 0;
            auction_day.refund_count_completed = 0;
            return Ok(());
        }

        let bidder_count = auction_day.bidder_count;
        require!(bidder_count > 0, ErrorCode::BidderCountMismatch);

        let loser_count = bidder_count
            .checked_sub(1)
            .ok_or(ErrorCode::MathOverflow)? as u64;
        let loser_sum = auction_day
            .total_bid_lamports
            .checked_sub(auction_day.highest_bid)
            .ok_or(ErrorCode::MathOverflow)?;
        let fee_pool = loser_count
            .checked_mul(ctx.accounts.config.loser_fee_lamports)
            .ok_or(ErrorCode::MathOverflow)?;
        require!(loser_sum >= fee_pool, ErrorCode::FeePoolTooLarge);
        let refund_pool = loser_sum
            .checked_sub(fee_pool)
            .ok_or(ErrorCode::MathOverflow)?;

        let vault_lamports = **ctx.accounts.vault.to_account_info().lamports.borrow();
        require!(
            vault_lamports >= auction_day.total_bid_lamports,
            ErrorCode::InsufficientVaultLamports
        );

        let recipient = &ctx.accounts.recipient;
        require!(
            recipient.key() == ctx.accounts.config.recipient_pubkey,
            ErrorCode::RecipientMismatch
        );

        let auction_day_key = auction_day.key();
        let seeds: &[&[u8]] = &[b"vault", auction_day_key.as_ref(), &[auction_day.vault_bump]];
        let signer_seeds: &[&[&[u8]]] = &[seeds];
        let cpi_ctx = CpiContext::new_with_signer(
            ctx.accounts.system_program.to_account_info(),
            Transfer {
                from: ctx.accounts.vault.to_account_info(),
                to: recipient.to_account_info(),
            },
            signer_seeds,
        );
        transfer(cpi_ctx, auction_day.highest_bid)?;

        auction_day.refund_pool_remaining = refund_pool;
        auction_day.fee_pool_remaining = fee_pool;
        auction_day.finalized = true;
        auction_day.refund_count_total = bidder_count
            .checked_sub(1)
            .ok_or(ErrorCode::MathOverflow)?;
        auction_day.refund_count_completed = 0;

        Ok(())
    }

    pub fn refund_batch<'info>(
        ctx: Context<'_, '_, '_, 'info, RefundBatch<'info>>,
        day_index: i64,
        bidders: Vec<Pubkey>,
    ) -> Result<()> {
        let auction_day = &mut ctx.accounts.auction_day;
        require!(auction_day.finalized, ErrorCode::NotFinalized);
        require!(auction_day.day_index == day_index, ErrorCode::DayMismatch);
        require!(
            ctx.accounts.vault.owner == &system_program::ID,
            ErrorCode::InvalidVaultOwner
        );
        let auction_day_key = auction_day.key();

        let expected_accounts = bidders.len().checked_mul(2).ok_or(ErrorCode::MathOverflow)?;
        require!(
            ctx.remaining_accounts.len() == expected_accounts,
            ErrorCode::InvalidRemainingAccounts
        );

        for (i, bidder_pubkey) in bidders.iter().enumerate() {
            let bid_receipt_info = &ctx.remaining_accounts[i * 2];
            let bidder_info = &ctx.remaining_accounts[i * 2 + 1];

            require!(
                bidder_info.key == bidder_pubkey,
                ErrorCode::BidderMismatch
            );

            let (expected_receipt, _bump) = Pubkey::find_program_address(
                &[
                    b"bid_receipt",
                    auction_day.key().as_ref(),
                    bidder_pubkey.as_ref(),
                ],
                ctx.program_id,
            );
            require!(
                bid_receipt_info.key == &expected_receipt,
                ErrorCode::BidReceiptMismatch
            );
            require!(
                bid_receipt_info.owner == ctx.program_id,
                ErrorCode::BidReceiptOwnerMismatch
            );

            let mut data_slice: &[u8] = &bid_receipt_info.data.borrow();
            let mut bid_receipt = BidReceipt::try_deserialize(&mut data_slice)?;

            require!(
                bid_receipt.auction_day == auction_day.key(),
                ErrorCode::BidReceiptMismatch
            );
            require!(bid_receipt.bidder == *bidder_pubkey, ErrorCode::BidderMismatch);

            if bid_receipt.refunded {
                continue;
            }

            if *bidder_pubkey == auction_day.winner {
                bid_receipt.refunded = true;
                auction_day.refund_count_completed = auction_day
                    .refund_count_completed
                    .checked_add(1)
                    .ok_or(ErrorCode::MathOverflow)?;
                write_bid_receipt(bid_receipt_info, &bid_receipt)?;
                continue;
            }

            require!(
                bid_receipt.amount > ctx.accounts.config.loser_fee_lamports,
                ErrorCode::InvalidBidAmount
            );
            let refund_amount = bid_receipt
                .amount
                .checked_sub(ctx.accounts.config.loser_fee_lamports)
                .ok_or(ErrorCode::MathOverflow)?;

            require!(
                auction_day.refund_pool_remaining >= refund_amount,
                ErrorCode::InsufficientRefundPool
            );
            require!(
                auction_day.fee_pool_remaining >= ctx.accounts.config.loser_fee_lamports,
                ErrorCode::InsufficientFeePool
            );

            let vault_lamports = **ctx.accounts.vault.to_account_info().lamports.borrow();
            require!(
                vault_lamports >= refund_amount + ctx.accounts.config.loser_fee_lamports,
                ErrorCode::InsufficientVaultLamports
            );

            let seeds: &[&[u8]] = &[b"vault", auction_day_key.as_ref(), &[auction_day.vault_bump]];
            let signer_seeds: &[&[&[u8]]] = &[seeds];

            let refund_ctx = CpiContext::new_with_signer(
                ctx.accounts.system_program.to_account_info(),
                Transfer {
                    from: ctx.accounts.vault.to_account_info(),
                    to: bidder_info.clone(),
                },
                signer_seeds,
            );
            transfer(refund_ctx, refund_amount)?;

            let fee_ctx = CpiContext::new_with_signer(
                ctx.accounts.system_program.to_account_info(),
                Transfer {
                    from: ctx.accounts.vault.to_account_info(),
                    to: ctx.accounts.cranker.to_account_info(),
                },
                signer_seeds,
            );
            transfer(fee_ctx, ctx.accounts.config.loser_fee_lamports)?;

            bid_receipt.refunded = true;
            auction_day.refund_pool_remaining = auction_day
                .refund_pool_remaining
                .checked_sub(refund_amount)
                .ok_or(ErrorCode::MathOverflow)?;
            auction_day.fee_pool_remaining = auction_day
                .fee_pool_remaining
                .checked_sub(ctx.accounts.config.loser_fee_lamports)
                .ok_or(ErrorCode::MathOverflow)?;
            auction_day.refund_count_completed = auction_day
                .refund_count_completed
                .checked_add(1)
                .ok_or(ErrorCode::MathOverflow)?;

            write_bid_receipt(bid_receipt_info, &bid_receipt)?;
        }

        Ok(())
    }
}

fn current_day_index(clock: &Clock) -> i64 {
    clock.unix_timestamp / SECONDS_PER_DAY
}

fn is_uninitialized_auction_day(auction_day: &AuctionDay) -> bool {
    auction_day.day_index == 0
        && !auction_day.finalized
        && auction_day.winner == Pubkey::default()
        && auction_day.highest_bid == 0
        && auction_day.bidder_count == 0
        && auction_day.refund_count_total == 0
        && auction_day.refund_count_completed == 0
        && auction_day.total_bid_lamports == 0
        && auction_day.refund_pool_remaining == 0
        && auction_day.fee_pool_remaining == 0
}

fn write_bid_receipt(account_info: &AccountInfo, receipt: &BidReceipt) -> Result<()> {
    let mut data = account_info.try_borrow_mut_data()?;
    let mut writer: &mut [u8] = &mut data;
    receipt.try_serialize(&mut writer)?;
    Ok(())
}

#[derive(Accounts)]
pub struct InitConfig<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(
        init,
        payer = payer,
        seeds = [b"config"],
        bump,
        space = Config::SPACE
    )]
    pub config: Account<'info, Config>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
#[instruction(day_index: i64)]
pub struct InitDay<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(
        init_if_needed,
        payer = payer,
        seeds = [b"auction_day", day_index.to_le_bytes().as_ref()],
        bump,
        space = AuctionDay::SPACE
    )]
    pub auction_day: Account<'info, AuctionDay>,
    #[account(
        init_if_needed,
        payer = payer,
        seeds = [b"vault", auction_day.key().as_ref()],
        bump,
        space = 0,
        owner = system_program::ID
    )]
    /// CHECK: PDA vault is system-owned (enforced by owner constraint + runtime checks).
    pub vault: UncheckedAccount<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
#[instruction(day_index: i64)]
pub struct PlaceBid<'info> {
    #[account(mut)]
    pub bidder: Signer<'info>,
    #[account(
        seeds = [b"config"],
        bump = config.bump
    )]
    pub config: Account<'info, Config>,
    #[account(
        init_if_needed,
        payer = bidder,
        seeds = [b"auction_day", day_index.to_le_bytes().as_ref()],
        bump,
        space = AuctionDay::SPACE
    )]
    pub auction_day: Account<'info, AuctionDay>,
    #[account(
        init_if_needed,
        payer = bidder,
        seeds = [b"vault", auction_day.key().as_ref()],
        bump,
        space = 0,
        owner = system_program::ID
    )]
    /// CHECK: PDA vault is system-owned (enforced by owner constraint + runtime checks).
    pub vault: UncheckedAccount<'info>,
    #[account(
        init_if_needed,
        payer = bidder,
        seeds = [b"bid_receipt", auction_day.key().as_ref(), bidder.key().as_ref()],
        bump,
        space = BidReceipt::SPACE
    )]
    pub bid_receipt: Account<'info, BidReceipt>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
#[instruction(day_index: i64)]
pub struct SettleDay<'info> {
    #[account(
        seeds = [b"config"],
        bump = config.bump
    )]
    pub config: Account<'info, Config>,
    #[account(
        mut,
        seeds = [b"auction_day", day_index.to_le_bytes().as_ref()],
        bump
    )]
    pub auction_day: Account<'info, AuctionDay>,
    #[account(
        mut,
        seeds = [b"vault", auction_day.key().as_ref()],
        bump = auction_day.vault_bump
    )]
    /// CHECK: PDA vault is system-owned (enforced by runtime check).
    pub vault: UncheckedAccount<'info>,
    /// CHECK: recipient is validated against config.
    #[account(mut)]
    pub recipient: AccountInfo<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
#[instruction(day_index: i64)]
pub struct RefundBatch<'info> {
    #[account(
        seeds = [b"config"],
        bump = config.bump
    )]
    pub config: Account<'info, Config>,
    #[account(
        mut,
        seeds = [b"auction_day", day_index.to_le_bytes().as_ref()],
        bump
    )]
    pub auction_day: Account<'info, AuctionDay>,
    #[account(
        mut,
        seeds = [b"vault", auction_day.key().as_ref()],
        bump = auction_day.vault_bump
    )]
    /// CHECK: PDA vault is system-owned (enforced by runtime check).
    pub vault: UncheckedAccount<'info>,
    #[account(mut)]
    pub cranker: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[account]
pub struct Config {
    pub recipient_pubkey: Pubkey,
    pub loser_fee_lamports: u64,
    pub min_increment_lamports: u64,
    pub bump: u8,
}

impl Config {
    pub const SPACE: usize = 8 + 32 + 8 + 8 + 1;
}

#[account]
pub struct AuctionDay {
    pub day_index: i64,
    pub finalized: bool,
    pub winner: Pubkey,
    pub highest_bid: u64,
    pub bidder_count: u32,
    pub refund_count_total: u32,
    pub refund_count_completed: u32,
    pub total_bid_lamports: u64,
    pub refund_pool_remaining: u64,
    pub fee_pool_remaining: u64,
    pub vault_bump: u8,
}

impl AuctionDay {
    pub const SPACE: usize = 8 + 8 + 1 + 32 + 8 + 4 + 4 + 4 + 8 + 8 + 8 + 1;
}

#[account]
pub struct BidReceipt {
    pub auction_day: Pubkey,
    pub bidder: Pubkey,
    pub amount: u64,
    pub refunded: bool,
}

impl BidReceipt {
    pub const SPACE: usize = 8 + 32 + 32 + 8 + 1;
}

#[error_code]
pub enum ErrorCode {
    #[msg("Missing bump seed")]
    MissingBump,
    #[msg("Day is too far ahead for init_day")]
    DayTooFarAhead,
    #[msg("Wrong day for bidding")]
    WrongDay,
    #[msg("Auction day already finalized")]
    AlreadyFinalized,
    #[msg("Bid does not meet minimum increment")]
    BidTooLow,
    #[msg("Bid must be greater than previous amount")]
    BidDecrease,
    #[msg("Invalid bid amount")]
    InvalidBidAmount,
    #[msg("Math overflow")]
    MathOverflow,
    #[msg("Auction day mismatch")]
    DayMismatch,
    #[msg("Settlement too early")]
    TooEarly,
    #[msg("Bidder count mismatch")]
    BidderCountMismatch,
    #[msg("Fee pool exceeds loser sum")]
    FeePoolTooLarge,
    #[msg("Insufficient vault lamports")]
    InsufficientVaultLamports,
    #[msg("Recipient does not match config")]
    RecipientMismatch,
    #[msg("Bidder mismatch")]
    BidderMismatch,
    #[msg("Bid receipt PDA mismatch")]
    BidReceiptMismatch,
    #[msg("Bid receipt owner mismatch")]
    BidReceiptOwnerMismatch,
    #[msg("Not finalized")]
    NotFinalized,
    #[msg("Invalid remaining accounts")]
    InvalidRemainingAccounts,
    #[msg("Insufficient refund pool")]
    InsufficientRefundPool,
    #[msg("Insufficient fee pool")]
    InsufficientFeePool,
    #[msg("Vault is not owned by the system program")]
    InvalidVaultOwner,
}
