
import asyncio
import json
import os
import time

import requests
from playwright.async_api import async_playwright

POSITIONS_FILE = '/Users/rey/.openclaw/workspace/futures-panel/positions.json'
GOOGLE_CHROME_PATH = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

# NOTE: Replace with your actual Telegram bot token and chat ID
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def load_positions():
    if not os.path.exists(POSITIONS_FILE):
        return []
    with open(POSITIONS_FILE, 'r') as f:
        return json.load(f)

async def fetch_price(variety):
    symbol_map = {
        "au": "nf_au", "ag": "nf_ag", "cu": "nf_cu",
        "rb": "nf_rb", "i": "nf_i", "m": "nf_m",
        "y": "nf_y", "CF": "nf_CF", "SR": "nf_SR",
        "TA": "nf_TA", "MA": "nf_MA"
    }
    
    prefix = "".join(filter(str.isalpha, variety))
    suffix = "".join(filter(str.isdigit, variety))
    
    sina_symbol = f"{symbol_map.get(prefix, prefix)}{suffix}"

    url = f"https://hq.sinajs.cn/list={sina_symbol}"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, executable_path=GOOGLE_CHROME_PATH)
            page = await browser.new_page()
            await page.goto(url, wait_until='domcontentloaded')
            content = await page.content()
            await browser.close()
            
            start_marker = f"var hq_{sina_symbol}=\""
            end_marker = "\";"
            
            data_start = content.find(start_marker)
            if data_start == -1:
                raise ValueError("Start marker not found")
            
            data_end = content.find(end_marker, data_start + len(start_marker))
            if data_end == -1:
                raise ValueError("End marker not found")
            
            data_string = content[data_start + len(start_marker):data_end]
            parts = data_string.split(',')
            
            if len(parts) > 7: # The 8th element (index 7) for current price
                price_str = parts[7] 
                return float(price_str)
            else:
                raise ValueError("Price string format not as expected")
            
    except Exception as e:
        print(f"Error fetching price for {variety}: {e}")
        return None

def calculate_rr(position, current_price):
    entry_price = float(position['entry_price'])
    direction = position['direction']
    support_price = float(position['support_price'])
    resistance_price = float(position['resistance_price'])

    rr_ratio = 0

    if direction == "long":
        if (current_price - support_price) != 0:
            rr_ratio = (resistance_price - current_price) / (current_price - support_price)
    elif direction == "short":
        if (resistance_price - current_price) != 0:
            rr_ratio = (current_price - support_price) / (resistance_price - current_price)
            
    return rr_ratio

def send_telegram_message(message_text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram bot token or chat ID not set. Skipping alert.")
        return

    alert_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "Markdown"
    }

    try:
        response = requests.post(alert_url, json=payload)
        response.raise_for_status() # Raise an exception for HTTP errors
        print("Telegram alert sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"Failed to send Telegram alert: {e}")

async def check_and_alert():
    positions = load_positions()
    alerts = []

    for position in positions:
        variety = position['variety']
        current_price = await fetch_price(variety)

        if current_price is None:
            alerts.append(f"Warning: Could not fetch price for {variety}")
            continue

        rr_ratio = calculate_rr(position, current_price)

        if rr_ratio > 2:
            alerts.append(
                f"*Alert for {variety} ({position['direction']})*: R/R is {rr_ratio:.2f} (Good!)"
            )
        elif rr_ratio < 0.5 and rr_ratio >= 0: # Only alert for positive R/R that is below 0.5, avoid alerting for negative R/R multiple times
             alerts.append(
                f"*Alert for {variety} ({position['direction']})*: R/R is {rr_ratio:.2f} (Warning!)"
            )
        elif rr_ratio < 0:
            alerts.append(
                f"*Alert for {variety} ({position['direction']})*: R/R is {rr_ratio:.2f} (Negative!)"
            )

    if alerts:
        full_message = "\n".join(alerts)
        send_telegram_message(full_message)

if __name__ == '__main__':
    asyncio.run(check_and_alert())
