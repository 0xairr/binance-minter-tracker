# Watchlist and provenance

`wallets.txt` is a snapshot of 4,942 unique lowercase addresses from the `Wallet`
column of the operator-supplied [public sheet](https://docs.google.com/spreadsheets/d/1jcKyWK1JJ8uBchTS3p1ok18DeB7yrDb2ZDxOaNYF73Q/htmlview).
It is a community research list, not a Binance endorsement.

`watchlist-source.json` records source, retrieval date, count and SHA-256 of the
LF-encoded file. `.gitattributes` preserves those bytes across platforms. The
scanner refuses count/hash mismatches. Review changes deliberately rather than
silently accepting edits to the public sheet.

`official-provenance.json` records the Binance skill/CLI versions and hashes
verified during initial development. It is a reference snapshot, not proof that
another machine's binaries match. Install from official sources and verify on
the machine that will sign.

No API keys, private wallet settings, execution records or third-party skills are
included here.
