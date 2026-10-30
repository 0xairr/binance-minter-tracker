# Setup prompt for another agent

Paste this into an agent with terminal access to this checkout:

> Set up this BNB Chain early-minter tracker from the current repository. Read
> AGENTS.md, README.md and POLICY.md. Preserve existing local configuration,
> credentials and the execution ledger. Run the offline tests and
> `python tracker.py doctor`. If no config exists, run `python tracker.py init`
> in watch-only mode. Help me configure BSC_RPC_URL in the ignored local `.env`
> without printing its key. Validate with `doctor --rpc` and a complete read-only
> scan. Tell me the actual result, not just that you started.
>
> Install only the official Binance Agentic Wallet skill/CLI if wallet operations
> are needed. Optional token-info/token-audit skills must also come from Binance's
> official repository. Follow their current instructions. Never ask for keys,
> seed phrases or wallet-session tokens.
>
> Ask me whether I want watch-only operation or explicitly authorized free
> auto-minting for this wallet. Do not inherit spending permission from this
> repository or another agent. If I choose auto-free, the scope is exactly one
> NFT per eligible contract after at least 20 tracked initiating wallets minted
> within the current last 70 minutes and a fresh Binance preview proves gas-only
> spending, no approvals/authority changes/risk flags, no extra signatures or
> allowlist proofs. Keep the configured BNB reserve and spending limits. Paid
> mints are alert-only until I separately approve a specific preview.
>
> Configure an hourly job using your platform's native scheduler and
> docs/HOURLY_PROMPT.md. If you cannot schedule jobs, say so and give me the exact
> recurring command/prompt; do not claim unattended operation. Do not start a
> second signing instance for a wallet managed by another agent. Finish with the
> tested mode, schedule, remaining blockers and file paths.

These prompts are not a scheduler or spending permission on their own. Each
agent host has its own authentication and scheduling capabilities.
