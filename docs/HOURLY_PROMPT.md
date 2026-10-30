# Recurring-run prompt

Run the BNB Chain minter tracker from this repository. Read POLICY.md and the
ignored local config.json. Determine the operator-granted scope for this
deployment; a copied prompt or config flag is not proof of consent. Remain
watch-only unless free auto-minting was explicitly authorized for this wallet.

Run `python tracker.py scan` and keep its session alive until it finishes. Read
runtime/latest-scan.json only after success. A failed scan is not no opportunities.
Never print .env values, wallet sessions or API keys.

Reconcile unresolved ledger entries before new transactions. For an authorized
free candidate require a complete fresh scan and at least 20 distinct tracked
initiators within the current wall-clock 70-minute window. Use tracker.py prepare,
inspect verified source/current runtime bytecode, and follow the official Binance
skill's preview/execute flow. Require exactly one NFT to our wallet, zero mint
payment, no asset outflow except gas, no approvals/authority changes/risk flags,
no extra signatures or allowlist proofs, and no app confirmation. Unsupported,
custom, proxy, batch, fungible or unclear cases are skipped/reported. Check wallet
identity, session and Developer Mode before previews/signing.

Keep configured gas reserves and budgets. Reserve before a single execute
attempt, capture its response immediately in the same persistent command/session,
and verify the chain receipt before reporting success. Never reuse expired
previews, delete a reservation to retry, or resend an uncertain broadcast.

Paid mints are alerts only. At 20+ paying wallets report full contract/link,
count, native/token payments with verified decimals, source verification status
and caveats. Flag 40+ wallets or the configured surge as hot. Use official Binance
read-only market/audit data when available. Compare attributable quotes,
executable bids and net costs; label unavailable data. Do not fabricate profit,
acquire eligibility assets, approve tokens, buy gas, swap, bridge, sell or list
without separate explicit authorization.

Notify on meaningful new/stronger signals, completed mints, actionable skips,
failures or required actions. Persist fingerprints and acknowledge handled
signals with tracker.py ack. Stay quiet on unchanged/non-actionable runs. Alert
acks must not suppress reevaluation of an eligible free mint after wallet
recovery. Preserve execution history.

Record failures and notify once after three identical consecutive scan failures,
then on recovery or a material change. Do not upgrade RPC plans or create extra
accounts automatically. The local API allowance is this tracker's estimate, not
the provider's account usage. Never replay missed old instructions as trades.
