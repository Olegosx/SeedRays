[Index](../../index.md) · [ADR Log](index.md) · [Русская версия](../../../ru/20-architecture/decisions/0027-gateway-fee-billing.md)

# ADR-0027: The Gateway Owner's Fee — Turnover, Invoices and Access Suspension

**Status:** accepted; extends [ADR-0008](0008-shared-registry-db.md) — the gateway gains a
third database, [ADR-0003](0003-single-process-supervised.md) — a third background task under
the supervisor, and [ADR-0005](0005-multi-user-model.md) — a user gains a second, independent
access state

## Context

The gateway owner takes a fee from the users: a percentage of the user's turnover over a
period, provided that turnover crossed a threshold. An invoice is issued at the end of the
period; failing to pay it in time closes access to the gateway and to the Application API.

Technically the gateway can already do everything this needs: the watcher observes transfers
on addresses ([ADR-0018](0018-range-scanning.md)), addresses come from a watch-only wallet
([ADR-0002](0002-watch-only-online-part.md)), users have a status, and mail is sent
([ADR-0020](0020-mail-provider.md)). What has to be decided is different: where the billing
data lives, how an incoming payment is attributed, and how a suspension for non-payment
coexists with the operator's administrative block.

One property of the blockchain shapes the whole mechanism: **an invoice does not exist as a
network entity**. A chain knows transfers only — who, to whom, how much. Payment-request links
(EIP-681 in EVM networks, the wallet links of TRON) merely prefill an address and an amount in
the payer's wallet: the payer is free to change the amount, and the network verifies nothing;
sending from an exchange there is no link to fill in at all. The payment memo, which in TON
distinguishes payers sharing one address, does not exist for TRC-20 transfers. Two consequences
are therefore taken as given: an underpayment is always possible, and a payment can only be
attributed by the receiving address.

## Decision

### 1. Turnover

- **A user's turnover over a period** is the sum of successful, finalized incoming operations
  on all their bound addresses, valued in USDT. Unfinished, provisional and failed rows are not
  part of it; an operation dropped by a chain reorganization leaves the turnover together with
  its row ([ADR-0021](0021-two-phase-scanning.md)).
- **Only assets from the operator's explicit list count** — by contract address, never by
  symbol. In the first stage these are stablecoins, valued one to one against USDT. The reason
  is hard: the asset catalog fills itself with whatever arrives on an address
  ([ADR-0010](0010-networks-assets-financial-data.md)) and a token symbol is an arbitrary
  string; without a list anyone could mint a token with the symbol "USDT", bury a user's
  addresses in it and produce an invoice out of thin air.
- **Transfers between the user's own addresses do not count**: this is money being moved,
  not income. The rule rests on the counterparty stored with every observed transfer — a
  row is an own move when the other side is a bound address of the same owner. Matching
  an incoming row to an outgoing one by amount would not do: an ordinary batch payout
  trips it, and anyone able to place two equal transfers into one transaction could
  switch their whole turnover off. Money arriving from another user of the gateway is
  income like any other: that the payer happens to be a client too is a coincidence, not
  a reason to waive the fee.
- **The period is a calendar month in UTC**, the same one gateway-wide.

### 2. The invoice

- When a period's turnover crosses the **threshold**, a **single invoice** is issued on the
  first day of the next month, for the **rate applied to the whole turnover** — not to the
  excess. Turnover below the threshold produces no invoice at all.
- An invoice carries the period, the turnover, the rate and threshold applied, the amount in
  USDT, the due date, and the payment network and address. **The rate, the threshold and the
  term are frozen into the invoice** as it is issued: changing the settings never rewrites
  invoices that already exist.
- **One invoice per "user + period"** — guaranteed by a unique key in the database, not by the
  carefulness of the code.
- The invoice is issued in the **user's payment network**; TRON by default, and the network can
  be changed from the cabinet while no invoice is unpaid — otherwise the details of an already
  issued invoice would move. If the owner has no master wallet for that network, no invoice is
  issued and the operator is notified: there is nowhere to issue it to.

### 3. The master wallet and payment attribution

- The owner enters the **watch-only xpub of the master wallet** for each payment network in the
  panel — the same account level as the users' wallets
  ([ADR-0014](0014-key-standards.md)). No secret is stored here either.
- Every user gets a **permanent address derived from the master wallet** — one per payment
  network, not one per invoice.
- **Why an address per user and not one shared address.** There is nothing to attribute a payer
  by on a shared address: invoice amounts of different users collide, the sender's address is
  not known in advance (people pay from an exchange or a personal wallet), and TRC-20 has no
  memo. A separate address makes attribution free and exact, and partial or early payments
  credit themselves. The second argument is privacy: on a shared address every user holding the
  details would see all the incoming payments of everyone — the owner's revenue, and by
  implication the shape of their client base.
- **Why not an address per invoice.** The address count would grow every month, while funds
  have to be swept from every address separately, paying the network fee each time (in TRON,
  with a TRX reserve on the address itself). One address per user keeps their number equal to
  the number of paying users.
- The master wallet goes through **the same gateway-wide xpub uniqueness check** as the users'
  wallets: a collision would give one address two owners, and the watcher would not know whose
  the incoming payment is. The rule is held by a check across the two databases rather than by
  a single schema key: the registry index covers the users' keys, the billing database covers
  the master wallets, and each of the two operations looks into both. The owner's key gets no
  row in the registry — there it would stand for a user who does not exist.

### 4. Observing payments

- **Billing watches its own addresses and shares nothing with the watcher** beyond the chain
  data source interface. The check is a per-address poll in "confirmed only" mode, once an
  hour, and only for addresses carrying an unpaid invoice. This is exactly the targeted task
  [ADR-0018](0018-range-scanning.md) kept per-address queries for while rejecting them as the
  watcher's main intake: a handful of addresses, a low frequency, and a cost that does not
  grow with the number of gateway users.
- **No second copy of the two-phase logic appears.** The watcher's two phases exist to show a
  pending payment immediately; billing does not need immediacy, and the provider itself can
  return finalized transfers only. Billing stores nothing provisional — so a chain
  reorganization has nothing to take back here.
- The check cursor lives on the invoice address: the next poll resumes from it with an
  overlap, and repeats are absorbed by the payment's idempotency key.
- A payment is credited **on finalization**, like everything else in the gateway.
- **Several transfers of one asset inside one transaction count as one payment**, summed.
  The per-address endpoint reports no ordinal for a transfer inside its transaction (the
  contract-event endpoint the watcher uses does), so there is nothing to tell them apart
  with in the idempotency key; summing keeps the key stable against a repeated poll and
  the amount right. An answer that held only part of a transaction is topped up by a
  later poll — upwards only, so a short answer can never devalue money already credited.
- **The amount is valued by the asset catalog's decimals, never by the answer's**: the
  decimals multiply the sum, so taking them from the provider would let it revalue this
  payment and every past one. A disagreement is logged; decimals outside a sane range
  make the answer unusable at the chain-source boundary.
- **The full amount** (within a configurable underpayment tolerance) settles the invoice and
  restores access automatically, without the operator.
- **An underpayment** leaves the invoice unpaid and access closed; the cabinet shows the
  remainder to be paid to the same address, and the owner is notified.
- **An overpayment** is credited against the next invoice; the owner is notified.
- **A foreign asset** on an invoice address (a stray or spam token) is recorded as a transfer,
  never credited to the invoice, and reported to the operator.
- **Manual confirmation by the operator** covers money that arrived outside the gateway. It
  requires a stated reason, lands in the security journal
  ([ADR-0023](0023-security-journal.md)) and has exactly the effect of a credited payment.
- The price of this design, accepted deliberately: **the provider sees the invoice addresses**.
  A per-address poll publishes them, unlike range scanning, and the provider can tie them to
  the gateway. For the owner's revenue addresses the risk is moderate, and it goes away with a
  node of one's own.
- A known limitation: "confirmed only" is a property of the source. TronGrid has it
  ([ADR-0015](0015-tron-provider.md)); in a network whose source does not, billing will have
  to count confirmation depth itself.

### 5. Storage — a separate billing database

The owner's billing data lives in a **separate `billing.db`** inside the gateway data
directory, with its own migration stream: master wallets, invoice addresses, invoices, payments
and their crediting, and the billing state of users.

- **Not the registry**: [ADR-0008](0008-shared-registry-db.md) forbids financial data in the
  shared registry database outright.
- **Not a user's database**: the owner's income must not depend on the survival of somebody
  else's database — one that gets moved, restored from a copy, and carried off wholesale into
  the archive when the user is deleted ([ADR-0024](0024-user-deletion-archive.md)).

### 6. Suspending access

- **The billing state is separate from the administrative block.** Access is open only when
  both are in order: otherwise an operator unblocking an offender would forgive their debt as a
  side effect, and paying the debt would lift the administrative block.
- Passing the due date puts the user into **"payment only" mode**: the Application API refuses
  calls entirely, while the cabinet keeps the invoices, the account read and the sign-out
  working. Closing the cabinet entirely is not an option — the user would lose access to the
  invoice and its details.
- The check lives in the **base dependency** of both route groups: a new route gets it by
  default, and bypassing it takes a deliberate switch to the payment-mode dependency. The
  check cannot be forgotten, only explicitly declined.
- Suspension and restoration are written to the security journal as **system** events: the
  gateway declares them itself, with no operator and no user action behind them.
- **Observation of the user's addresses does not stop**: their payers keep paying, and a gap
  would tear a hole in the history and in the next period's turnover.
- Access is restored **automatically** once the full amount is credited.
- **The access state is reconciled with the invoices, never driven by an event.** A pass
  brings every user's state in line with what the invoices say right now: whoever holds an
  overdue invoice is suspended, whoever has nothing awaiting money is let back in. Acting
  on the rows one call happened to change would make any failure permanent — the invoice
  is marked already, so the next pass would no longer see that user, and an overdue
  invoice would sit beside an open gateway for good. The operator's manual confirmation
  stays immediate: it is a synchronous action whose result the operator must see in the
  answer, and the reconciliation confirms it afterwards.

### 7. The billing task

A third background task under the same supervisor as the API server and the watcher
([ADR-0003](0003-single-process-supervised.md)). Once an hour it does two things in order:
first it checks for payments (otherwise an invoice paid yesterday would have time to turn
overdue), then it issues the invoices of the finished period, marks the overdue ones and
reconciles access. The payment check itself falls into two parts: polling the provider and
storing what it reports, then crediting the payments the database already holds. The second
part never depends on the outcome of the first — it touches no network — so an unreachable
provider cannot leave money that arrived in advance uncredited and turn a paid invoice
overdue.
Issuing is not tied to a calendar day: a pass always bills the last period that has ended, so
a gateway that was down on the first of the month issues at its next start. Repeats duplicate
nothing — idempotency rests on the invoice key, not on the schedule.

### 8. Settings

The parameters are registry settings ([ADR-0016](0016-config-layers.md)) on the panel's
settings page: the fee switch (**off** by default — an installation must not start issuing
invoices on its own), the rate, the threshold, the payment term, the per-network list of
turnover assets, the underpayment tolerance and the notification parameters. Master wallets are
entered there too but stored in the billing database — by the same logic that keeps a user's
xpub in the user's database.

Email notifications: an issued invoice, a reminder a configurable number of days before the due
date, a suspension and a restoration of access; to the owner — underpayments, overpayments and
foreign assets.

## Alternatives Considered

- **One shared master-account address:** rejected — the payer cannot be attributed on it
  (amounts collide, the sender is unknown, TRC-20 has no memo), and the owner's turnover becomes
  visible to every client.
- **An address per invoice:** rejected — the address count would grow every month, and sweeping
  each one costs a network fee.
- **Invoices in the user's database:** rejected — the owner's income would depend on the
  survival of somebody else's database, which is moved, restored and archived.
- **Invoices in the shared registry:** rejected — forbidden by
  [ADR-0008](0008-shared-registry-db.md).
- **The fee charged on the excess above the threshold only:** rejected by the owner — "a
  percentage of the whole turnover, from the threshold up" is the simpler rule to explain.
- **Reusing the existing block status:** rejected — it would merge two unrelated decisions of
  the operator, so lifting one would silently lift the other.
- **Closing the cabinet entirely on non-payment:** rejected — the user would lose access to the
  invoice and its details, that is, to the means of paying.
- **Turnover over every catalog asset:** rejected — a counterfeit token carrying the symbol
  "USDT" would produce an invoice out of thin air.
- **A configurable period length:** deferred — a calendar month covers the need, while a
  setting would drag in partial periods and moving boundaries.

## Explicitly Deferred

- **Exchange rates.** For now turnover counts stablecoins only, one to one; the gateway has no
  external rate source. Rates will bring turnover in native coins and other fluctuating assets
  along with an **exempt amount** — the deduction covering the native coin a user tops their own
  addresses up with as fuel for sweeping funds (the mechanism is designed now, defaults to zero
  and switches on together with rates).
- **A known gap of the first stage:** a user accepting payments in the native coin pays no fee —
  their turnover is invisible to the gateway. Only rates close it.
- **Refunding an overpayment in money:** an overpayment lives as credit until the next invoice.

## Consequences

- The data directory gains a third database and a third migration stream; a backup of the
  directory still covers everything.
- The watcher is left untouched: all it shares with billing is the chain data source
  interface and the provider access settings, which moved into the chains layer — the key and
  the request rate were always a gateway-wide resource rather than the watcher's property.
- Two components poll the provider independently, but billing's share is tiny: one request per
  address with an unpaid invoice, once an hour.
- The access check appears in both route groups — the application one and the user one — next
  to the status check.
- The gateway owner becomes a payee inside their own product: a miscalculation or a false
  overdue shows up to the user as lost access, so the price of an error here is higher than
  usual and manual payment confirmation is a mandatory safeguard.
- Funds are swept from many addresses; in TRON every transfer costs a fee and a reserve of the
  native coin on the source address.
- A user's first, partial period counts in full: the threshold filters out the small change.
- Gateway-wide xpub uniqueness extends to the owner's master wallets — through two facing
  checks, so a single schema key no longer guarantees it; entering one key from both sides at
  once is serialized by an in-process lock, the way derivation indexes already are (ADR-0003).

## Related

- [Data Model](../data-model.md)
- [Watcher](../components/watcher.md)
- [HTTP API](../components/http-api.md)
- [User Cabinet Scenarios](../../50-frontend/user-cabinet.md)
- [Operator Panel Scenarios](../../50-frontend/operator-panel.md)
- [ADR-0008: Shared Registry Database](0008-shared-registry-db.md)
- [ADR-0021: Two-Phase Scanning](0021-two-phase-scanning.md)
