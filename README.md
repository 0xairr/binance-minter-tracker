# Binance early-minter tracker

An agent-neutral BNB Chain mint scanner with a pinned 4,942-wallet watchlist,
distinct-wallet signals, payment detection, and a durable execution ledger.
Works from a local checkout with Codex, Claude Code, Cursor, Gemini CLI, or another
agent that can run Python and the official Binance wallet CLI.

**Python scans and records state. It never signs or broadcasts transactions.**
An AI agent must separately review verified source and the actual Binance preview
before executing an explicitly authorized free mint. New clones are watch-only;
this repository does not carry permission to spend from any wallet.

## Quick start

Requires Python 3.11+. The scanner has no third-party Python dependencies.

```sh
git clone https://github.com/0xairr/binance-minter-tracker.git
cd binance-minter-tracker
python tracker.py init
python tracker.py doctor
```

Set `BSC_RPC_URL` in the generated `.env` file to a BNB Mainnet endpoint. For
Alchemy, copy the HTTPS endpoint from your app's BNB Mainnet dashboard. Enter the
key locally, never in a committed file or agent prompt. An environment variable
with the same name takes precedence over `.env`.

```sh
python tracker.py doctor --rpc
python tracker.py scan
```

Read `runtime/latest-scan.json` after a successful scan. A scan can take several
minutes; keep its process alive until it exits. Failed scans are not zero-signal
results, and stale reports must never be used for trades.

## Set up with another AI agent

Open this folder in your agent and paste [the setup prompt](docs/AGENT_PROMPT.md).
Agents should read [AGENTS.md](AGENTS.md) and [POLICY.md](POLICY.md), then use
[the recurring-run prompt](docs/HOURLY_PROMPT.md) with their native scheduler.
The repository does not install a scheduler automatically and is not tied to a
particular model, browser, connector, or hosted service.

Watch-only operation needs no wallet login or Binance CLI. For wallet previews
and authorized free mints, install only the official tools:

```sh
npm install -g @binance/agentic-wallet@1.12.0
npx skills add https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet
```

Optional read-only [token-info](https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/query-token-info)
and [token-audit](https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/query-token-audit)
skills also come from Binance's official repository. Third-party skills are not
bundled. Check upstream requirements when updating the skill/CLI.

Sign in through the official skill's `baw auth signin` / `auth verify` flow and
complete QR pairing in Binance App. Enable Developer Mode in Agentic Wallet
settings when needed. Never export wallet sessions, keys or seed phrases.
Sessions and Developer Mode expire; signing must stop until usable again.

To configure a wallet while retaining watch-only mode, a fresh deployment may use:

```sh
python tracker.py init --wallet YOUR_BNB_ADDRESS
```

`init` refuses to overwrite an active config. If already initialized, edit the
ignored `config.json` locally. Only after the operator explicitly authorizes
free auto-minting may the agent set `auto_free_mints_authorized` to `true`.
A fresh deployment can instead use `init --wallet YOUR_BNB_ADDRESS --auto-free`
after that authorization. Paid auto-minting is always disabled.

## Default rules

| Setting | Default |
| --- | --- |
| Chain | BNB Smart Chain mainnet, 56 |
| Lookback / confirmations | 70 minutes / 20 blocks |
| Free trigger | 20 distinct tracked initiating wallets |
| Paid alert | 20 distinct paying wallets; never automatic |
| Hot signal | 40 wallets, or a qualifying 15-minute surge |
| Quantity | One NFT per eligible contract |
| Gas reserve | At least 0.003 BNB after reservation |
| Per-mint / daily gas allowance | 0.0002 / 0.001 BNB |
| Daily auto-mints | At most five |
| New deployment | Watch-only |

Gas allowances are conservative preflight reservations, not an on-chain max-fee
guarantee. UTC dates determine budgets. The ledger prevents duplicate contract
attempts and retains uncertain outcomes. Use one signing deployment per wallet.
When handing a wallet to another agent, stop the previous signer and transfer
its ledger securely; do not create parallel fresh signing ledgers.

Free candidates require a fresh successful simulation showing exactly one NFT
arriving, no asset payment, no approvals/authority changes, no risk flags, and no
extra signatures or allowlist proofs. Source must be verified and reviewed.
Only unambiguous direct ERC-721 and simple single-transfer ERC-1155 calls are
prepared. Custom, batch, relayed, proxy, signature, allowlist, fungible, and unclear
calls are reported/skipped. Signing uses only the official `baw` CLI after every
condition in POLICY.md passes.

## Data and coverage

The public [source sheet](https://docs.google.com/spreadsheets/d/1jcKyWK1JJ8uBchTS3p1ok18DeB7yrDb2ZDxOaNYF73Q/htmlview)
is pinned in `data/wallets.txt`; the manifest records its count, column and SHA-256.
The list is not silently refreshed. See [data notes](data/README.md).

Alchemy endpoints use paginated Transfers API discovery, recent raw-log
cross-checks, and actual chain receipt verification. Other providers use raw logs.
The public RPC fallback can be rate-limited/unavailable; configure a provider.
BNB's public dataseed is used for block reads, not chain-wide `eth_getLogs`.

Sender must equal tracked recipient, excluding unsolicited airdrops. Relayers,
smart accounts, nonstandard/non-indexed events and mints sent elsewhere can be
missed. Zero native value is not proof of a free mint: outgoing token events and
a fresh wallet simulation must also be checked. Source verification is not an
audit. Missing prices/floors/liquidity stay unavailable; listings alone are not
executable exit prices.

The local Alchemy meter stops at 27M estimated CUs per UTC calendar month. It is
not the provider's account meter and excludes other applications' usage. Actual
usage varies with traffic and retries. The code never changes your plan.

## Files and commands

| Path / command | Purpose |
| --- | --- |
| `config.example.json` | Shared defaults; no wallet or spending permission |
| `config.json`, `.env` | Private local settings; ignored by git |
| `AUTHORIZATION.local.md` | Optional local operator authorization record; ignored |
| `runtime/state.sqlite` | Cache, signals and execution ledger; ignored |
| `runtime/latest-scan.json` | Most recent complete scan |
| `runtime/scan-progress.json` | Current progress |
| `python tracker.py prepare --contract ADDRESS --wallet ADDRESS` | Prepare a supported zero-value call; never signs |
| `python tracker.py reserve --contract ADDRESS --max-gas-bnb AMOUNT` | Persist gas/duplicate reservation |
| `python tracker.py record --contract ADDRESS --status STATUS` | Record/reconcile a reserved attempt |
| `python tracker.py ack --contract ADDRESS` | Acknowledge a handled signal |
| `python tracker.py status` | Show local settings/execution summaries |

Schedule hourly with your agent's native scheduler. This is not a hosted 24/7
service. Gaps longer than the overlapping window can miss opportunities; never
execute stale backlog instructions or reuse expired previews.

## Validation

```sh
python -m unittest discover -s tests -v
```

Tests use synthetic receipts and isolated temporary state. No RPC key, wallet
session, or transaction is required. GitHub Actions tests Windows and Linux.
CI contains no signing or recurring mint workflow.
