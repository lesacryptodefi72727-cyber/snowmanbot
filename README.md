# SnowmanBot — buttons and payment upgrade

Version: `snowman-2026-10-24-buttons-payments`

## Upload and deploy

Extract the ZIP and upload **all five application files** to the root of your GitHub repository, replacing the existing files:

- `bot.py`
- `crypto.py`
- `storefront.py` — new file; required
- `requirements.txt` — includes the free local QR generator
- `Dockerfile` — copies all three Python files and checks their syntax during build

Upload the extracted files, not the ZIP itself. Keep your existing hosting environment variables and PostgreSQL database. Commit the changes, then deploy the latest commit. On Render use `/health` as the health-check path. One running replica handles both Telegram bots.

The schema upgrade runs automatically and adds tables/columns. It keeps your existing users, products, orders, images and payment settings. Do not delete or recreate your database.

Press Telegram's **Start** button in each bot. In the admin bot, all normal actions use buttons and guided input. There is no command format to memorize. Tokens remain private hosting variables.

## Bank accounts

1. Open **Bank accounts & rates** → **Add bank account**.
2. Choose a currency.
3. Choose text instructions or a bank-details screenshot.
4. Enter bank name, account name and account number / IBAN / mobile-money number. Leading zeroes are preserved. A screenshot is shown to customers; its text is not read automatically. Enter the account number separately so the Copy button copies the correct number.
5. Enter your conversion rate: local currency units per 1 USD.
6. Review and tap **Save & enable bank**.

The account appears at checkout immediately after saving. A bank needs both payment details and a rate. The bank list shows accounts that are disabled or missing a rate. Existing legacy text settings remain available; open their bank setup to add a copyable number and screenshot.

Open an existing account to update it or disable/enable it. **Keep current value** avoids retyping unchanged fields. New checkouts use the updated bank; existing orders retain their original instructions and amount so a customer already paying is not sent conflicting details. Start a fresh checkout to see the update.

Customers get the bank screenshot when provided, text details, and native Copy buttons for account number, account name, bank, amount and payment reference. They tap **Upload receipt** and send their photo/document without a caption command.

## Crypto payments

Supported networks:

| Asset | Network |
|---|---|
| USDT | TRC20 / TRON only |
| SOL | Solana mainnet native SOL |
| BTC | Bitcoin mainnet |
| TON | TON mainnet native TON |
| ETH | Ethereum mainnet native ETH |

Every active crypto quote has an address QR, **Copy address**, **Copy exact amount**, and a 30-minute countdown. The QR contains the address only; the customer must enter the copied exact amount and use the stated network. Network fees are separate. The chat countdown refreshes approximately every 15 seconds while the service is running, subject to Telegram rate limits. It survives restarts. Expired quotes remove the payment-copy buttons but keep hash/receipt recovery options.

The bot automatically looks for incoming payments using public/free-tier services: Blockstream, Blockscout plus your Ethereum RPC, Solana RPC, TON Center, and TronGrid. Candidate transfers are verified separately before confirmation. Checks include the receiving address, exact atomic amount, network, transaction success, invoice time and required confirmations. Bitcoin requires 3 confirmations; Ethereum requires 12; Solana requires finalization; TON requires an indexed masterchain transaction; USDT requires a confirmed official-token TRC20 transfer.

The bot sends a transfer-detected message and a payment-confirmed message after successful checks. Scans run approximately every 45 seconds after the previous pass. Public APIs can throttle or lag, so timing depends on provider availability and transaction volume. History scans are bounded. If a payment is not found automatically, use **Submit transaction hash** or **Upload receipt**. A known hash is still checked if the history index is unavailable. Pasting a bare hash also offers an order choice when needed. For TON, the receiving wallet's transaction hash is used; automatic discovery finds that hash.

Late, underpaid, wrong-network, token-contract/internal ETH transfers, or unsupported transaction formats are not automatically credited. Receipt upload is available for manual review. Expiry stops new payments to the quote; a transfer included on chain within the invoice window can finish confirming afterward. Review questionable or late payments before asking the customer to pay again.

No paid QR or deposit API is required by the code. Free providers have limits. `COINGECKO_API_KEY` remains optional for price conversion; a free Demo key can be used if keyless pricing is throttled.

## Review bank and crypto payments

Open **Orders & payment review**, then select an order. The admin sees the uploaded receipt, method, amount, hash, status and audit history.

- **Mark payment received**: check your bank/wallet, enter your review note, then confirm approval.
- **Decline payment**: enter the reason, then confirm. The customer receives the reason. Declining does not refund an actual transfer.
- **Add / change transaction hash**: attach a wallet hash to a crypto order when needed. Manual crypto approval requires a hash and prevents reusing a transaction already credited to another order.
- **Check blockchain**: retry verification for a crypto order.
- **Mark delivered / tracking**: send fulfillment details for a paid order.
- **Refund Stars**: confirm a Stars refund for an eligible paid digital order.

Manual approval is recorded as an admin decision. It is not labelled as network verification. A screenshot alone never causes automatic credit. Normal notifications and decisions are stored in PostgreSQL; duplicate confirmation attempts do not create duplicate credits or normal confirmation jobs.

## Products, reminders and notifications

Use **Add product** to enter its type, name, price, description, image and digital delivery if applicable. Review it, edit fields with buttons, then publish or save as a draft. Use **Manage products** to edit existing products or hide/publish them. Physical product prices are in USD and local checkout uses your saved rate. Digital products continue to use Telegram Stars.

Use **Daily reminder** to choose a UTC time and enter a message. `09:00 UTC` is `10:00` in Nigeria. Reminders and new-product marketing alerts go only to subscribed users. Payment/order updates remain enabled independently. Notification queue buttons allow deliberate retry of failed/uncertain sends; uncertain sends may already have arrived.

## Existing variables

Required: `MAIN_BOT_TOKEN`, `ADMIN_BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL`.

Use your numeric personal Telegram user ID in `ADMIN_IDS`, not a bot ID or username. An unapproved account is shown its own ID and cannot access management actions.

Wallet variables: `USDT_ADDRESS`, `SOL_ADDRESS`, `BTC_ADDRESS`, `TON_ADDRESS`, `ETH_ADDRESS`. ETH and SOL also require `ETH_RPC_URL` and `SOL_RPC_URL` pointing at the stated mainnets. Optional free-tier keys: `TONCENTER_API_KEY`, `TRONGRID_API_KEY`, `COINGECKO_API_KEY`. `SUPPORT_CONTACT` controls the support message. Keep wallet keys/seed phrases out of the app; only public receiving addresses are used.

Legacy `LOCAL_PAYMENTS_JSON` and `FX_RATES_JSON` are initial seeds, not live updates to existing settings. Use the bank setup buttons for updates.

Timers, scanning, polling and reminders require the service to be running. An HTTP health endpoint does not keep a sleeping hosting plan awake.

## Validation

Syntax/build checks and local integration tests use an embedded PostgreSQL engine with simulated Telegram/network responses. QR images are decoded independently for all five assets. Bank setup/visibility, snapshots, receipts, authorization, manual and network confirmation, replay prevention, timers, product edits, reminders, notification buttons and HTTP readiness are checked. These checks do not constitute a live Telegram deployment or a real-money test; test the deployed bot in Telegram before using it with customers.
