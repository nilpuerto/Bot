PRYM  — FULL MVP SPEC
📌 1. Introduction

Prym  is an intelligent automated trading assistant for prediction markets, designed to identify high-impact news events and execute profitable trades early.

The system combines:

Real-time news monitoring
AI-based event analysis
Market data from Polymarket API
Execution + control via Telegram
Lightweight AI processing using Mistral AI
🎯 Goal

Capture high-probability, low-price opportunities before market reaction.

🏗️ 2. System Architecture
🔹 Core Stack
Language: Python
Bot Interface: Telegram Bot API
AI: Mistral API
Data Sources:
News APIs (RSS, Twitter/X scraping)
Polymarket API
Storage: Supabase → scalable to PostgreSQL
🔹 Architecture Flow
News Sources → Filter → AI Analysis → Market Matching → Signal → Trade Execution → Telegram Control
⚙️ 3. Core Components
3.1 News Ingestion Module

Sources:

RSS feeds (Reuters, BBC, etc.)
Twitter/X (keywords)
Optional OSINT APIs
Function:
Fetch news every 30–60 seconds
Normalize text
Remove duplicates
3.2 Hard Filter (NO AI — critical)

This reduces cost and noise.

Conditions:
Contains strong keywords:
"election", "war", "breaking", "approved", "ban", "dies"
Published within last 5 minutes
Trusted source

👉 Only ~5% of news passes

3.3 AI Analysis (Mistral)
Prompt Example:
Analyze this news:

- Is it relevant to a prediction market?
- Which market?
- Impact: bullish / bearish
- Urgency score (1-10)

Return JSON only.
Output Example:
{
  "market": "Trump wins election",
  "impact": "bullish",
  "urgency": 9
}
3.4 Market Matching Engine

Using Polymarket API:

Search related market
Extract:
current price
liquidity
recent price movement
3.5 Trading Strategy Engine
Entry Conditions:
Price < 0.35
Volume increasing
AI urgency ≥ 7
Position Size:
3–5% of total balance
Risk Rules:
Max 5 concurrent trades
Stop loss: -10%
Take profit: +10–20%
3.6 Trade Execution Module

Modes:

Manual
Semi-auto
Auto

Execution:

Buy shares via Polymarket API
Store trade in DB
🗄️ 4. Database Structure (SQLite MVP)
Tables:
users
id
telegram_id
balance
mode (safe/semi/auto)
trades
id
market
entry_price
amount
status (open/closed)
pnl
timestamp
signals
id
news_title
market
urgency
created_at
📲 5. Telegram Bot Design
🎨 UX (important)

Telegram allows:

Inline buttons
Markdown formatting
Emojis
Clean dashboards
Apple style
Darkmode

👉 You can make it look very premium.

🔹 Commands
/start

Intro message:

What Prym Signals does
Risk disclaimer
Setup steps
/info

Returns:

Balance
Total PnL
Win rate
Active trades
/trades

List of open trades:

#1 Trump election
Entry: 0.22
Now: 0.27
PnL: +5€
/signals

Recent opportunities

/mode

Switch between:

safe
semi
auto
/close <id>

Close trade manually

/settings
Risk %
Max trades
Auto mode threshold
🚨 6. Signal Message Example
🚨 PRYM SIGNAL

News: "Candidate wins key state"
Market: Trump wins election
Price: 0.24
AI Score: 9/10

[✅ Buy] [❌ Ignore]
🤖 7. Automation Modes
SAFE
Alerts only
SEMI (recommended)
User confirms trades
AUTO
Bot trades automatically if:
urgency ≥ 9
strong volume
🔐 8. Security
Use separate wallet
Never store private keys in plain text
Use environment variables
Limit API permissions
Add trade limits
💸 9. Cost Optimization (VERY IMPORTANT)

To minimize AI costs:

Use hard filter BEFORE AI
Only analyze top news (~5–10/day)
Cache repeated topics

👉 Result: near-zero cost with Mistral

🚀 10. MVP Development Plan
Day 1
Telegram bot setup
Basic commands
Day 2
News ingestion + filtering
Day 3
Mistral integration
Day 4
Polymarket connection
Day 5
Signal system
Day 6
Trading execution
🧠 11. Key Advantage

This system is NOT:

a generic trading bot

It is:
👉 a news-driven alpha detector

⚠️ 12. Reality Check
No system guarantees profit
Edge comes from:
speed
filtering quality
discipline
🎯 Final Concept

Prym  =

“A real-time AI-powered news sniper for prediction markets"

All in darkmode, apllemode, telegram good design, and all good text and specifyc