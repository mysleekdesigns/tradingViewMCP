# Video notes: "I Stopped Trading On Feeling; It's Why I'm Profitable"

> [!warning] Untrusted source
> YouTube, channel Tom Sosnoff (@SosnoffonMoney), uploaded 2026-10-23, 8:19 long, about 586k views when read on 2026-11-01.
> URL: https://www.youtube.com/watch?v=M5jKMWpg9Tg. It promotes Lossdog (lossdog.com), the author's new venture.
> The author founded thinkorswim and tastytrade; brokerages earn from trade volume.
> These notes summarise the claims. They are not findings of this project. I saw no text aimed at AI agents.
> I read the transcript from YouTube's own transcript panel in the owner's Chrome (description and chapters
> via CrawlForge `extract_embedded_state`). No transcript was fetched through a robots-disallowed route.

## Topic

Position sizing, not entries or exits. A viewer asked whether to size up a trade when research makes him
confident. Context is options premium selling on US equities, not spot crypto.

## The four sizing systems, as the video presents them

| System | How it works | Video's verdict |
|---|---|---|
| Fixed fractional | Same % of the account at risk on every trade; dollar size grows and shrinks with the account | Author's preferred base: simple, cuts risk in drawdowns, makes blow-ups unlikely. Flaw: ignores edge strength and treats quiet and wild assets the same. Basis of "trade small, trade often". |
| Kelly criterion | Size from win probability and payoff to maximise long-run compounding | Inputs are never right and traders overestimate their edge. Full-Kelly drawdowns of 50%+ are common; practitioners bet ½ or ¼ Kelly. Only plausible in inefficient markets. |
| Volatility-adjusted | Size moves inversely with volatility so each position carries similar risk; core tool of trend-following and risk-parity funds | Strongly endorsed, combined with trade small / trade often. Flaw: backward-looking, so a gap or vol spike hits before it adjusts. |
| Martingale | Double after every loss so one win recovers everything | Always blows up. 10 straight losses = a bet 1,023× the original; starting at 1% risk, 7 straight losses wipe the account (1+2+…+64 = 127%). |

## The author's own rules (options selling, % of net liquidation value)

| Item | Value |
|---|---|
| Defined-risk trade, buying power per trade | 0.5–1.5% |
| Undefined-risk trade, buying power per trade | 3–7% |
| Daily total theta (premium sellers) | 0.1–0.3% |
| Total buying power used, low IV rank | 30–50% |
| Total buying power used, high IV rank | 40–65% |
| Proof of concept before each step up in size | at least 3 months of returns at each tranche level |

Note the twist: each trade stays the same small size, but **total** exposure rises when implied volatility is
high, because option premium is expensive then and the seller is paid more. This is specific to selling
options; it is the opposite of volatility-adjusted sizing for a spot buyer.

## Answer to the viewer

Sizing up on conviction is acceptable as a one-off at 2–3× normal size. 5–10× is ruled out; the author says
prop and floor traders who stepped up to a quarter, half or all of their account on perceived opportunity
did not survive.

## Edge and the casino analogy

- Claimed trading edge is tiny: "pennies" per $100,000 of notional, so sticking to the mechanics matters.
- Casinos have an edge of roughly 2.5–6% and still cap bets (example: $25 minimum, $2,500 maximum table),
  because they want many small bets.
- Closing claim: if you trade small and often, the counterparty has no edge over you.

## Assessment for this project

- **Sound:** keep each position small; treat conviction as worth 2–3× at most; never Martingale (in crypto,
  averaging down into a falling coin); fractional Kelly at most, since edge estimates are unreliable; scale
  up only after a sustained proof period (compare PLAN.md's 4–8 weeks of forward paper trading).
- **Does not follow:** "trade small, trade often removes the counterparty's edge". Small size controls the
  risk of ruin; it does not create an edge. With a negative edge after costs (e.g. the ONDO 5m RSI 30/50 rule,
  journal `2026-11-01-ondousdt-rsi-cross-30-exit-50-5m.md`: about +0.14% gross vs about 0.30% round-trip cost
  per trade), trading more often loses faster. Kelly for that rule is zero.
- **Crypto translation (illustrative, not advice):** risk about 0.5–1.5% of the account per trade
  (position × stop distance), total exposure well under 100%, smaller positions when realized vol is high.
  The lab found volatility targeting has no edge as a return source, but it reduces drawdown (ONDO design
  window: vol-targeted max DD 19% vs buy-and-hold 44%), which is the use the video endorses.
- Possible input to PLAN.md Phase 20 (paper-trading risk controls): per-trade risk cap, total exposure cap,
  no averaging down, size steps gated on a proof period. Owner decision; nothing implemented.
