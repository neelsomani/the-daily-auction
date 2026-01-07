"use client";

import { Buffer } from "buffer";
import { useEffect, useMemo, useState } from "react";
import {
  PublicKey,
  SystemProgram,
  Transaction,
  TransactionInstruction,
} from "@solana/web3.js";
import { useConnection, useWallet } from "@solana/wallet-adapter-react";
import { WalletMultiButton } from "@solana/wallet-adapter-react-ui";

const SITE_URL = "https://www.thedailyauction.app";
const SHARE_URL = "https://www.thedailyauction.com";
const GITHUB_URL = "https://github.com/neelsomani/the-daily-auction";
const X_INTENT = "https://twitter.com/intent/tweet";
const SECONDS_PER_DAY = 86400;

function secondsUntilNextUtcMidnight(nowMs) {
  const now = new Date(nowMs);
  const next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1, 0, 0, 0));
  return Math.max(0, Math.floor((next.getTime() - nowMs) / 1000));
}

function formatCountdown(totalSeconds) {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(
    2,
    "0"
  )}`;
}

function i64ToLeBytes(value) {
  const buf = Buffer.alloc(8);
  let temp = BigInt(value);
  for (let i = 0; i < 8; i += 1) {
    buf[i] = Number(temp & 0xffn);
    temp >>= 8n;
  }
  return buf;
}

function readU64(buffer, offset) {
  const view = buffer.subarray(offset, offset + 8);
  let value = 0n;
  for (let i = 7; i >= 0; i -= 1) {
    value = (value << 8n) + BigInt(view[i]);
  }
  return value;
}

function readU32(buffer, offset) {
  const view = buffer.subarray(offset, offset + 4);
  let value = 0;
  for (let i = 3; i >= 0; i -= 1) {
    value = (value << 8) + view[i];
  }
  return value;
}

function decodeAuctionDay(buffer) {
  let cursor = 8;
  const dayIndex = Number(buffer.readBigInt64LE(cursor));
  cursor += 8;
  const finalized = buffer[cursor] === 1;
  cursor += 1;
  const winnerBytes = buffer.subarray(cursor, cursor + 32);
  const winner = new PublicKey(winnerBytes).toBase58();
  cursor += 32;
  const highestBid = readU64(buffer, cursor);
  cursor += 8;
  const bidderCount = readU32(buffer, cursor);
  cursor += 4;
  const refundCountTotal = readU32(buffer, cursor);
  cursor += 4;
  const refundCountCompleted = readU32(buffer, cursor);
  cursor += 4;
  const totalBidLamports = readU64(buffer, cursor);
  cursor += 8;
  const refundPoolRemaining = readU64(buffer, cursor);
  cursor += 8;
  const feePoolRemaining = readU64(buffer, cursor);
  cursor += 8;
  const vaultBump = buffer[cursor];
  return {
    dayIndex,
    finalized,
    winner,
    highestBid,
    bidderCount,
    refundCountTotal,
    refundCountCompleted,
    totalBidLamports,
    refundPoolRemaining,
    feePoolRemaining,
    vaultBump,
  };
}

function u64ToLeBytes(value) {
  const buf = Buffer.alloc(8);
  let temp = BigInt(value);
  for (let i = 0; i < 8; i += 1) {
    buf[i] = Number(temp & 0xffn);
    temp >>= 8n;
  }
  return buf;
}

async function anchorDiscriminator(name) {
  const data = new TextEncoder().encode(`global:${name}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Buffer.from(digest).subarray(0, 8);
}

async function buildPlaceBidData(dayIndex, lamports) {
  const disc = await anchorDiscriminator("place_bid");
  return Buffer.concat([disc, i64ToLeBytes(dayIndex), u64ToLeBytes(lamports)]);
}

function auctionPdas(programId, dayIndex, bidder) {
  const programKey = new PublicKey(programId);
  const [auctionDay] = PublicKey.findProgramAddressSync(
    [Buffer.from("auction_day"), i64ToLeBytes(dayIndex)],
    programKey
  );
  const [vault] = PublicKey.findProgramAddressSync([Buffer.from("vault"), auctionDay.toBuffer()], programKey);
  const [bidReceipt] = PublicKey.findProgramAddressSync(
    [Buffer.from("bid_receipt"), auctionDay.toBuffer(), bidder.toBuffer()],
    programKey
  );
  const [config] = PublicKey.findProgramAddressSync([Buffer.from("config")], programKey);
  return { programKey, auctionDay, vault, bidReceipt, config };
}

function decodeBidReceipt(buffer) {
  let cursor = 8;
  cursor += 32;
  cursor += 32;
  const amount = readU64(buffer, cursor);
  cursor += 8;
  const refunded = buffer[cursor] === 1;
  return { amount, refunded };
}

async function fetchAuctionDay(programId, dayIndex, rpcUrl) {
  const programKey = new PublicKey(programId);
  const [auctionDay] = PublicKey.findProgramAddressSync(
    [Buffer.from("auction_day"), i64ToLeBytes(dayIndex)],
    programKey
  );
  const body = {
    jsonrpc: "2.0",
    id: 1,
    method: "getAccountInfo",
    params: [auctionDay.toBase58(), { encoding: "base64" }],
  };
  const resp = await fetch(rpcUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await resp.json();
  const value = json?.result?.value;
  if (!value) {
    return null;
  }
  const data = value.data?.[0];
  if (!data) {
    return null;
  }
  const buffer = Buffer.from(data, "base64");
  return decodeAuctionDay(buffer);
}

async function fetchBidReceipt(programId, dayIndex, bidder, rpcUrl) {
  const { bidReceipt } = auctionPdas(programId, dayIndex, bidder);
  const body = {
    jsonrpc: "2.0",
    id: 1,
    method: "getAccountInfo",
    params: [bidReceipt.toBase58(), { encoding: "base64" }],
  };
  const resp = await fetch(rpcUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await resp.json();
  const value = json?.result?.value;
  if (!value) {
    return null;
  }
  const data = value.data?.[0];
  if (!data) {
    return null;
  }
  const buffer = Buffer.from(data, "base64");
  return decodeBidReceipt(buffer);
}

export default function Home() {
  const { connection } = useConnection();
  const { publicKey, sendTransaction, connected } = useWallet();
  const [secondsRemaining, setSecondsRemaining] = useState(null);
  const [winner, setWinner] = useState("Loading...");
  const [currentPrice, setCurrentPrice] = useState("--");
  const [winnerPubkey, setWinnerPubkey] = useState(null);
  const [userBidLamports, setUserBidLamports] = useState(0n);
  const [mounted, setMounted] = useState(false);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [isTosOpen, setIsTosOpen] = useState(false);
  const [bidAmount, setBidAmount] = useState("0.1");
  const [bidStatus, setBidStatus] = useState("idle");
  const [bidSignature, setBidSignature] = useState("");
  const [copyStatus, setCopyStatus] = useState("idle");
  const [bidError, setBidError] = useState("");
  const disabled = useMemo(
    () => String(process.env.NEXT_PUBLIC_DISABLE_SITE || "").toLowerCase() === "true",
    []
  );

  useEffect(() => {
    setMounted(true);
    setSecondsRemaining(secondsUntilNextUtcMidnight(Date.now()));
    const tick = () => setSecondsRemaining(secondsUntilNextUtcMidnight(Date.now()));
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, []);

  const loadAuctionDay = async () => {
    const rpcUrl = process.env.NEXT_PUBLIC_RPC_URL || "https://api.devnet.solana.com";
    const programId = process.env.NEXT_PUBLIC_AUCTION_PROGRAM_ID;
    if (!programId) {
      setWinner("Missing program ID");
      return;
    }
    const dayIndex = Math.floor(Date.now() / 1000 / SECONDS_PER_DAY);
    const data = await fetchAuctionDay(programId, dayIndex, rpcUrl);
    if (!data || data.highestBid === 0n) {
      setWinner("No bids yet");
      setWinnerPubkey(null);
      setCurrentPrice("0.10 SOL");
      setBidAmount("0.10");
      return;
    }
    setWinner(data.winner);
    setWinnerPubkey(data.winner);
    const sol = Number(data.highestBid) / 1_000_000_000;
    const nextMin = sol + 0.1;
    setCurrentPrice(`${nextMin.toFixed(2)} SOL`);
    setBidAmount(nextMin.toFixed(2));
    if (publicKey) {
      const receipt = await fetchBidReceipt(programId, dayIndex, publicKey, rpcUrl);
      if (receipt) {
        setUserBidLamports(receipt.amount);
      } else {
        setUserBidLamports(0n);
      }
    }
  };

  useEffect(() => {
    loadAuctionDay().catch(() => setWinner("Unavailable"));
    const refresh = setInterval(() => {
      loadAuctionDay().catch(() => setWinner("Unavailable"));
    }, 15000);
    return () => clearInterval(refresh);
  }, [publicKey]);

  const placeBid = async () => {
    if (!publicKey) {
      setBidError("Connect your wallet to bid.");
      return;
    }
    if (!bidAmount) {
      setBidAmount("0.1");
    }
    const programId = process.env.NEXT_PUBLIC_AUCTION_PROGRAM_ID;
    if (!programId) {
      setBidError("Missing program ID.");
      return;
    }
    const solAmount = Number(bidAmount);
    if (!Number.isFinite(solAmount) || solAmount < 0.1) {
      setBidError("Bid must be at least 0.1 SOL.");
      return;
    }
    const previousSol = Number(userBidLamports) / 1_000_000_000;
    if (solAmount <= previousSol) {
      setBidError("Bid must be higher than your current bid.");
      return;
    }
    setBidStatus("pending");
    setBidError("");
    const dayIndex = Math.floor(Date.now() / 1000 / SECONDS_PER_DAY);
    const lamports = Math.floor(solAmount * 1_000_000_000);
    const pdas = auctionPdas(programId, dayIndex, publicKey);

    const keys = [
      { pubkey: publicKey, isSigner: true, isWritable: true },
      { pubkey: pdas.config, isSigner: false, isWritable: false },
      { pubkey: pdas.auctionDay, isSigner: false, isWritable: true },
      { pubkey: pdas.vault, isSigner: false, isWritable: true },
      { pubkey: pdas.bidReceipt, isSigner: false, isWritable: true },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ];

    const data = await buildPlaceBidData(dayIndex, lamports);
    const instruction = new TransactionInstruction({
      programId: pdas.programKey,
      keys,
      data,
    });

    try {
      const transaction = new Transaction().add(instruction);
      const signature = await sendTransaction(transaction, connection);
      setBidSignature(signature);
      setBidStatus("submitted");
      connection
        .confirmTransaction(signature, "finalized")
        .then(async () => {
          setBidStatus("success");
          await loadAuctionDay();
          setTimeout(() => {
            loadAuctionDay().catch(() => setWinner("Unavailable"));
          }, 2000);
        })
        .catch(() => {
          setBidStatus("submitted");
        });
    } catch (err) {
      setBidStatus("error");
      setBidError("Transaction failed. Please retry.");
    }
  };

  const shortenKey = (key) => {
    if (!key) return "";
    return `${key.slice(0, 4)}...${key.slice(-4)}`;
  };

  const copyWinner = async () => {
    if (!winnerPubkey) return;
    try {
      await navigator.clipboard.writeText(winnerPubkey);
      setCopyStatus("copied");
      setTimeout(() => setCopyStatus("idle"), 1500);
    } catch {
      setBidError("Copy failed.");
    }
  };

  const shareText = useMemo(() => {
    const priceText = currentPrice === "--" ? "0.10 SOL" : currentPrice;
    return `This website is auctioning itself off for tomorrow! The price is ${priceText}.`;
  }, [currentPrice]);

  const explorerCluster = useMemo(() => {
    const rpcUrl = process.env.NEXT_PUBLIC_RPC_URL || "";
    if (rpcUrl.includes("devnet")) return "devnet";
    if (rpcUrl.includes("testnet")) return "testnet";
    if (rpcUrl.includes("mainnet")) return "mainnet";
    return "custom";
  }, []);

  return (
    <main className="page">
      {/*
        Keep the share copy short so it fits in a single tweet.
      */}
      <header className="banner">
        <div className="banner__left">
          <h1 className="banner__title">The Daily Auction</h1>
          <p className="banner__warning">
            The website below is untrusted. Double check any outbound links before you click.
          </p>
          <p className="banner__winner-line">
            <span>Today’s winner:</span> <strong>{winner}</strong>
          </p>
        </div>
        <div className="banner__right">
          <div className="banner__stats">
            <div className="banner__stat">
              <span className="label">Auction ends in</span>
              <span className="value">
                {mounted && secondsRemaining !== null ? formatCountdown(secondsRemaining) : "--:--:--"}
              </span>
            </div>
            <div className="banner__stat">
              <span className="label">Current price</span>
              <span className="value">{currentPrice}</span>
            </div>
          </div>
          <div className="banner__cta">
            <button className="primary" onClick={() => setIsModalOpen(true)}>
              Participate in today’s auction
            </button>
          </div>
        </div>
      </header>

      <section className="frame">
        {disabled ? (
          <div className="disabled">
            <p className="disabled__title">Today’s site has been temporarily disabled.</p>
            <p className="disabled__body">
              Check back tomorrow, or participate in the auction to win control of the next day’s site.
            </p>
          </div>
        ) : (
          <iframe
            className="frame__iframe"
            title="Daily Auction Site"
            src={SITE_URL}
            sandbox="allow-same-origin allow-scripts allow-forms"
            referrerPolicy="no-referrer"
          />
        )}
      </section>

      <footer className="footer">
        <div className="footer__links">
          <a className="footer__link" href={GITHUB_URL} target="_blank" rel="noreferrer">
            <svg
              className="footer__icon"
              viewBox="0 0 24 24"
              aria-hidden="true"
              focusable="false"
            >
              <path
                fill="currentColor"
                d="M12 2c-5.52 0-10 4.58-10 10.24 0 4.52 2.87 8.35 6.84 9.7.5.1.68-.23.68-.5 0-.25-.01-1.07-.02-1.95-2.78.62-3.37-1.23-3.37-1.23-.45-1.18-1.11-1.49-1.11-1.49-.9-.64.07-.63.07-.63 1 .07 1.52 1.05 1.52 1.05.9 1.57 2.36 1.12 2.94.86.09-.67.34-1.12.62-1.38-2.22-.26-4.56-1.14-4.56-5.08 0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.32.1-2.75 0 0 .84-.28 2.75 1.05.8-.23 1.65-.34 2.5-.34.85 0 1.7.12 2.5.34 1.9-1.33 2.74-1.05 2.74-1.05.56 1.43.21 2.49.1 2.75.64.72 1.03 1.63 1.03 2.75 0 3.95-2.35 4.82-4.59 5.07.35.31.67.92.67 1.86 0 1.34-.01 2.42-.01 2.75 0 .28.18.61.69.5 3.96-1.35 6.83-5.18 6.83-9.7C22 6.58 17.52 2 12 2Z"
              />
            </svg>
            GitHub
          </a>
          <button className="footer__link footer__link--button" type="button" onClick={() => setIsTosOpen(true)}>
            Terms of Service
          </button>
          <a
            className="footer__link footer__link--x"
            href={`${X_INTENT}?text=${encodeURIComponent(shareText)}&url=${encodeURIComponent(SHARE_URL)}`}
            target="_blank"
            rel="noreferrer"
          >
            <svg
              className="footer__icon"
              viewBox="0 0 24 24"
              aria-hidden="true"
              focusable="false"
            >
              <path
                fill="currentColor"
                d="M18.9 3H21l-6.4 7.3L22 21h-6.6l-4.2-5.4L6.4 21H3.2l6.9-7.9L2 3h6.8l3.8 5 4.3-5Z"
              />
            </svg>
            Post to X
          </a>
        </div>
      </footer>

      {isModalOpen ? (
        <div className="modal" role="dialog" aria-modal="true" aria-labelledby="auction-modal-title">
          <div className="modal__backdrop" onClick={() => setIsModalOpen(false)} />
          <div className="modal__panel">
            <button className="modal__close" onClick={() => setIsModalOpen(false)} aria-label="Close">
              ×
            </button>
            <h2 id="auction-modal-title">Participate in today’s auction</h2>
            <p className="modal__copy">
              Connect your wallet to place a bid. The winner gets control of tomorrow’s website.
              Losing bidders receive refunds minus a 0.0001 SOL processing fee.
            </p>
            <div className="modal__actions">
              <div className="modal__row">
                <WalletMultiButton className="wallet-button" />
                <div className="modal__winner">
                  <span className="label">Current winner</span>
                  <button className="modal__winner-value" type="button" onClick={copyWinner}>
                    {winnerPubkey
                      ? copyStatus === "copied"
                        ? "Copied"
                        : `${shortenKey(winnerPubkey)}${
                            publicKey && winnerPubkey === publicKey.toBase58() ? " (you)" : ""
                          }`
                      : "No bids yet"}
                  </button>
                </div>
              </div>
              <div className="modal__summary">
                <div>
                  <span className="label">Your current bid</span>
                  <span className="value">
                    {(Number(userBidLamports) / 1_000_000_000).toFixed(2)} SOL
                  </span>
                </div>
                <div>
                  <span className="label">Additional due</span>
                  <span className="value">
                    {(() => {
                      const target = Number(bidAmount || 0);
                      const prev = Number(userBidLamports) / 1_000_000_000;
                      const delta = Math.max(0, target - prev);
                      return `${delta.toFixed(2)} SOL`;
                    })()}
                  </span>
                </div>
              </div>
              <div className="modal__field">
                <label htmlFor="bidAmount">Bid amount (SOL)</label>
                <input
                  id="bidAmount"
                  type="number"
                  min="0.1"
                  step="0.1"
                  placeholder="0.1"
                  value={bidAmount}
                  onChange={(event) => setBidAmount(event.target.value)}
                />
              </div>
              <button
                className="primary"
                type="button"
                onClick={placeBid}
                disabled={!connected || bidStatus === "pending"}
              >
                {bidStatus === "pending" ? "Placing bid..." : "Place bid"}
              </button>
            </div>
            {bidStatus === "submitted" || bidStatus === "success" ? (
              <p className="modal__note">
                Bid submitted.{" "}
                {bidSignature ? (
                  <a
                    className="modal__link"
                    href={`https://explorer.solana.com/tx/${bidSignature}?cluster=${explorerCluster}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    View on explorer
                  </a>
                ) : null}
                {bidStatus === "success" ? " (confirmed)" : ""}
              </p>
            ) : null}
            {bidError ? <p className="modal__error">{bidError}</p> : null}
          </div>
        </div>
      ) : null}
      {isTosOpen ? (
        <div className="modal" role="dialog" aria-modal="true" aria-labelledby="tos-modal-title">
          <div className="modal__backdrop" onClick={() => setIsTosOpen(false)} />
          <div className="modal__panel modal__panel--tos">
            <button className="modal__close" onClick={() => setIsTosOpen(false)} aria-label="Close">
              ×
            </button>
            <h2 id="tos-modal-title">Terms of Service</h2>
            <div className="modal__body">
              <p className="modal__copy">Galois Holdings LLC</p>
              <p className="modal__copy">
                By accessing or using any website, platform, or service operated by Galois Holdings LLC ("Galois"),
                you agree to these Terms.
              </p>
              <h3 className="modal__subhead">No Rights; Absolute Discretion</h3>
              <p className="modal__copy">
                Use of the service is a privilege, not a right. Galois retains sole and absolute discretion over the
                service and all auctions, bids, outcomes, and associated websites or digital properties.
              </p>
              <p className="modal__copy">
                Galois may, at any time, for any reason or no reason, with or without notice:
              </p>
              <ul className="modal__list">
                <li>
                  Disable, suspend, remove, modify, or terminate any website or digital property, including one
                  designated as an auction “winner”;
                </li>
                <li>Invalidate, reverse, cancel, or disregard any auction result or bid;</li>
                <li>Deny access to the service to any user.</li>
              </ul>
              <p className="modal__copy">No user is entitled to an explanation.</p>
              <h3 className="modal__subhead">No Reliance; No Refunds</h3>
              <p className="modal__copy">
                You agree not to rely on the continued availability, operation, or value of the service or any
                auctioned website. All bids, payments, and fees are final and non-refundable.
              </p>
              <h3 className="modal__subhead">Assumption of Risk</h3>
              <p className="modal__copy">
                All participation is entirely at your own risk. The service is experimental and may change, fail, or
                terminate at any time.
              </p>
              <h3 className="modal__subhead">No Warranties</h3>
              <p className="modal__copy">
                The service is provided “as is” and “as available,” without warranties of any kind, express or implied.
              </p>
              <h3 className="modal__subhead">Limitation of Liability</h3>
              <p className="modal__copy">
                To the maximum extent permitted by law, Galois shall not be liable for any damages of any kind arising
                from or related to the service, any auction, or any disabling or termination of a website or digital
                property.
              </p>
              <h3 className="modal__subhead">Indemnification</h3>
              <p className="modal__copy">
                You agree to indemnify and hold harmless Galois Holdings LLC from any claims arising from your use of
                the service.
              </p>
              <h3 className="modal__subhead">Governing Law</h3>
              <p className="modal__copy">These Terms are governed by the laws of the State of Delaware.</p>
              <h3 className="modal__subhead">Changes</h3>
              <p className="modal__copy">Galois may modify these Terms at any time. Continued use constitutes acceptance.</p>
            </div>
          </div>
        </div>
      ) : null}
    </main>
  );
}
