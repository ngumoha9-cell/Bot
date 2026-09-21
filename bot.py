import os
import threading
import time
import random
import math
import re
import json
import requests
from bs4 import BeautifulSoup
from flask import Flask
import telebot
from telebot import types

# 1. Initialize Flask Keep-Alive Server for Cloud Hosting
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is active and running 24/7!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# 2. Initialize Telegram Bot
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set!")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

user_pools = {}

RANGES = {
    "range_1": (100, 500, "100 to 500"),
    "range_2": (500, 1000, "500 to 1,000"),
    "range_3": (1000, 4000, "1,000 to 4,000"),
    "range_4": (4000, 10000, "4,000 to 10,000")
}

def load_team_stats():
    if os.path.exists("team_stats.json"):
        with open("team_stats.json", "r") as f:
            return json.load(f)
    return {}

def poisson_pmf(k, lam):
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)

def calculate_sharp_picks(events):
    pool = []
    margin = 0.93  
    stats_db = load_team_stats()
    default_att, default_def = 1.25, 1.15

    for match in events:
        home, away = match["home"], match["away"]
        home_data = stats_db.get(home, {"att": default_att, "def": default_def})
        away_data = stats_db.get(away, {"att": default_att, "def": default_def})
        
        home_xg = max(0.5, (home_data["att"] * away_data["def"]) * 1.12)
        away_xg = max(0.5, (away_data["att"] * home_data["def"]) * 0.98)

        matrix = {}
        for h in range(7):
            for a in range(7):
                matrix[(h, a)] = poisson_pmf(h, home_xg) * poisson_pmf(a, away_xg)

        p_home_win = sum(matrix[(h, a)] for h in range(7) for a in range(7) if h > a)
        p_draw = sum(matrix[(h, a)] for h in range(7) for a in range(7) if h == a)
        p_away_win = sum(matrix[(h, a)] for h in range(7) for a in range(7) if h < a)
        p_dc_1x = p_home_win + p_draw
        p_dc_x2 = p_away_win + p_draw
        p_dc_12 = p_home_win + p_away_win
        p_dnb_home = p_home_win / (p_home_win + p_away_win) if (p_home_win + p_away_win) > 0 else 0
        p_dnb_away = p_away_win / (p_home_win + p_away_win) if (p_home_win + p_away_win) > 0 else 0
        p_o25 = sum(matrix[(h, a)] for h in range(7) for a in range(7) if h + a >= 3)
        p_u25 = 1.0 - p_o25
        p_btts_yes = sum(matrix[(h, a)] for h in range(1, 7) for a in range(1, 7))
        p_btts_no = 1.0 - p_btts_yes

        candidates = [
            (p_home_win, f"{home} Win"),
            (p_away_win, f"{away} Win"),
            (p_draw, "Draw"),
            (p_dc_1x, f"{home} or Draw (1X)"),
            (p_dc_x2, f"{away} or Draw (X2)"),
            (p_dc_12, "Any Team Win (12)"),
            (p_dnb_home, f"{home} Draw No Bet"),
            (p_dnb_away, f"{away} Draw No Bet"),
            (p_o25, "Over 2.5 Goals"),
            (p_u25, "Under 2.5 Goals"),
            (p_btts_yes, "BTTS - Yes"),
            (p_btts_no, "BTTS - No")
        ]

        for prob, label in candidates:
            if 0.22 <= prob <= 0.88: 
                implied_odds = round((1 / prob) * margin, 2)
                if 1.25 <= implied_odds <= 4.50:
                    pool.append({
                        "match": f"{home} vs {away}", 
                        "pick": label, 
                        "odds": implied_odds
                    })
    return pool

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text(message):
    if message.text.startswith('/'):
        return
        
    bot.reply_to(message, "📝 *Parsing text fixtures with stats model...*", parse_mode="Markdown")
    matches = []
    lines = [line.strip() for line in message.text.split('\n') if line.strip()]
    for line in lines:
        parts = re.split(r'\s+vs\s+|\s+-\s+', line, flags=re.IGNORECASE)
        if len(parts) == 2:
            home, away = parts[0].strip(), parts[1].strip()
            if len(home) > 2 and len(away) > 2:
                matches.append({"home": home, "away": away})
                
    if not matches:
        bot.send_message(message.chat.id, "⚠️ Format each line as `Home vs Away`.")
        return
        
    new_pool = calculate_sharp_picks(matches)
    if not new_pool:
        bot.send_message(message.chat.id, "⚠️ Matches parsed, but no viable odds could be calculated.")
        return
        
    if message.chat.id not in user_pools:
        user_pools[message.chat.id] = []
        
    existing_matches = {leg["match"] for leg in user_pools[message.chat.id]}
    added_count = 0
    
    for pick in new_pool:
        user_pools[message.chat.id].append(pick)
        if pick["match"] not in existing_matches:
            existing_matches.add(pick["match"])
            added_count += 1
            
    total_unique_matches = len(existing_matches)
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(text=f"🎯 {label} Odds", callback_data=key) for key, (_, _, label) in RANGES.items()]
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton(text="🗑️ Clear Matches", callback_data="clear_pool"))
    
    bot.send_message(
        message.chat.id, 
        f"✅ **Vault Updated**\nAdded {added_count} matches.\n📦 **Total unique games in vault:** {total_unique_matches}\n\nSelect odds bracket below:", 
        reply_markup=markup, 
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == 'clear_pool')
def handle_clear(call):
    user_pools[call.message.chat.id] = []
    bot.edit_message_text("🗑️ **Vault Cleared.**", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('range_'))
def handle_range(call):
    pool = user_pools.get(call.message.chat.id, [])
    if not pool:
        bot.answer_callback_query(call.id, "Vault is empty.")
        return
        
    min_o, max_o, label = RANGES[call.data]
    bot.edit_message_text(f"🍳 *Cooking mixed-market slip for {label} odds...*", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="Markdown")
                          
    slip = None
    final_odds = 0
    random.shuffle(pool)
    
    for _ in range(50000):
        k = random.randint(4, min(35, len(pool)))
        candidate = random.sample(pool, k)
        seen, deduped = set(), []
        for c in candidate:
            if c["match"] not in seen:
                seen.add(c["match"])
                deduped.append(c)
                
        total = math.prod(x["odds"] for x in deduped)
        if min_o <= total <= max_o:
            slip = deduped
            final_odds = round(total, 2)
            break
            
    if not slip:
        bot.send_message(call.message.chat.id, f"❌ Could not reach {label} odds safely.")
        return
        
    lines = [f"🔥 *Mixed-Market Ticket ({final_odds} Total Odds)*\n"]
    for idx, leg in enumerate(slip, 1):
        lines.append(f"{idx}. *{leg['match']}*\n   Pick: `{leg['pick']}` @ `~{leg['odds']}`")
        
    lines.append(f"\n📋 *Total Legs:* {len(slip)}")
    lines.append(f"💰 *Calculated Multiplier:* `{final_odds}`")
    bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="Markdown")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "⚡ *Cloud Stats Bot Online*\n\nSend your match list (`Team A vs Team B`), and I will cook your accumulator using statistical expected goals.", parse_mode="Markdown")

# 3. Background Thread for Telegram Polling (Crucial for Gunicorn)
def start_telegram_polling():
    while True:
        try:
            print("Starting Telegram bot polling...")
            bot.infinity_polling(timeout=30, long_polling_timeout=10)
        except Exception as e:
            print(f"Polling error: {e}. Retrying...")
            time.sleep(5)

polling_thread = threading.Thread(target=start_telegram_polling)
polling_thread.daemon = True
polling_thread.start()

if __name__ == "__main__":
    run_flask()
    
