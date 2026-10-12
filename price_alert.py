#!/usr/bin/env python3
"""
Real-Time Price Alert System for Angel One SmartAPI - ENHANCED VERSION
Monitors watchlist for price movements and sends richly formatted Telegram alerts.
Adapted formatting ideas from stock_monitor system while keeping Angel One backend.
"""

import os
import sys
import json
import time
from datetime import datetime
from SmartApi import SmartConnect
import pyotp
import subprocess

def _get_hermes_config(key):
    """Get configuration value from Hermes config"""
    try:
        result = subprocess.run(['hermes', 'config', 'get', key], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None

def initialize_environment():
    """Initialize environment variables from Hermes config if not set"""
    # Only set if not already in environment
    if not os.getenv("ANGEL_ONE_API_KEY"):
        val = _get_hermes_config("angelone.api_key")
        if val:
            os.environ["ANGEL_ONE_API_KEY"] = val
            
    if not os.getenv("ANGEL_ONE_CLIENT_ID"):
        val = _get_hermes_config("angelone.client_id")
        if val:
            os.environ["ANGEL_ONE_CLIENT_ID"] = val
            
    if not os.getenv("ANGEL_ONE_PASSWORD"):
        val = _get_hermes_config("angelone.password")
        if val:
            os.environ["ANGEL_ONE_PASSWORD"] = val
            
    if not os.getenv("ANGEL_ONE_TOTP_SECRET"):
        val = _get_hermes_config("angelone.totp_secret")
        if val:
            os.environ["ANGEL_ONE_TOTP_SECRET"] = val

# Watchlist configuration - customize as needed
WATCHLIST = [
    {"symbol": "RELIANCE", "name": "Reliance Industries", "target_up": 1350, "target_down": 1280},
    {"symbol": "TCS", "name": "Tata Consultancy Services", "target_up": 2350, "target_down": 2230},
    {"symbol": "INFY", "name": "Infosys", "target_up": 1160, "target_down": 1100},
    {"symbol": "HDFCBANK", "name": "HDFC Bank", "target_up": 750, "target_down": 700},
    {"symbol": "ICICIBANK", "name": "ICICI Bank", "target_up": 1450, "target_down": 1360},
]

# Symbol to token mapping (pre-resolved for faster lookup)
SYMBOL_TOKEN_MAP = {
    "RELIANCE": "2885",
    "TCS": "11536",
    "INFY": "1594",
    "HDFCBANK": "1333",
    "ICICIBANK": "4963",
}

def get_ltp(symbol):
    """Get LTP for a symbol using direct SmartApi call (price only)."""
    try:
        client = _get_client()
        symbol_token = SYMBOL_TOKEN_MAP.get(symbol)
        
        if not symbol_token:
            print(f"Warning: No pre-resolved token for {symbol}", file=sys.stderr)
            return None
            
        # Exchange is NSE for all our stocks
        exchange = "NSE"
        
        # Call ltpData API
        ltp_response = client.ltpData(exchange, symbol, symbol_token)
        
        if not ltp_response.get("status"):
            return None
        
        data = ltp_response.get("data", {})
        ltp = data.get("ltp")
        
        if ltp is not None:
            return float(ltp)
        return None
        
    except Exception as e:
        print(f"Error getting LTP for {symbol}: {e}", file=sys.stderr)
        return None

def _get_client():
    """Get or create SmartApi client with session caching"""
    # Global client cache to avoid recreating session every time
    # Using function attribute for simple caching
    if not hasattr(get_ltp, "_client"):
        get_ltp._client = None
        get_ltp._client_timestamp = 0
    
    CLIENT_CACHE_TIMEOUT = 300  # 5 minutes
    now = time.time()
    
    # Return cached client if it's less than 5 minutes old
    if get_ltp._client is not None and (now - get_ltp._client_timestamp) < CLIENT_CACHE_TIMEOUT:
        return get_ltp._client
    
    # Create new client
    api_key = os.getenv("ANGEL_ONE_API_KEY")
    client_id = os.getenv("ANGEL_ONE_CLIENT_ID")
    password = os.getenv("ANGEL_ONE_PASSWORD")
    totp_secret = os.getenv("ANGEL_ONE_TOTP_SECRET")
    
    if not all([api_key, client_id, password, totp_secret]):
        raise ValueError("Missing required Angel One environment variables")
    
    client = SmartConnect(api_key=api_key)
    
    # Generate session with TOTP
    totp = pyotp.TOTP(totp_secret).now()
    session_data = client.generateSession(client_id, password, totp)
    
    if not session_data.get("status"):
        raise Exception(f"Failed to generate session: {session_data.get('message', 'Unknown error')}")
    
    # Cache the client
    get_ltp._client = client
    get_ltp._client_timestamp = now
    return client

def load_alert_state():
    """Load alert state from file to prevent duplicate alerts."""
    try:
        if os.path.exists(os.path.join(os.path.dirname(__file__), 'alert_state.json')):
            with open(os.path.join(os.path.dirname(__file__), 'alert_state.json'), 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_alert_state(state):
    """Save alert state to file."""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'alert_state.json'), 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Error saving alert state: {e}", file=sys.stderr)

def format_alert_message(name, symbol, current_price, target_level, alert_type):
    """Format a rich alert message adapted from stock_monitor system."""
    if alert_type == "UP":
        emoji = "🚀"
        action = "HIT UPTARGET"
        deviation = ((current_price / target_level) - 1) * 100
        deviation_sign = "+"
    else:  # DOWN
        emoji = "💥"
        action = "FELL BELOW DOWNTARGET"
        deviation = ((target_level / current_price) - 1) * 100
        deviation_sign = "++"  # Using ++ as in original script for downside deviation
    
    # Calculate absolute deviation
    abs_deviation = abs(deviation)
    
    message = f"""<b>{emoji} {name} ({symbol}) {action}! {emoji}</b>
<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>

<b>💹 Current Price:</b> <code>₹{current_price:.2f}</code>
<b>🎯 {'UPTARGET' if alert_type == 'UP' else 'DOWNTARGET'}:</b> <code>₹{target_level:.2f}</code>
<b>📊 Deviation:</b> <code>{deviation_sign}{abs_deviation:.1f}%</code>

<b>⏰ Time:</b> <code>{datetime.now().strftime('%d %b %Y %H:%M:%S IST')}</code>

<i>💡 Trade responsibly. This is an automated alert from your Hermes Agent.</i>"""
    return message

def check_price_alerts():
    """Check watchlist for price alerts and return list of alert messages."""
    # Initialize environment from Hermes config
    initialize_environment()
    
    alert_state = load_alert_state()
    alert_messages = []
    
    # Print debug info to stderr
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking price alerts for {len(WATCHLIST)} stocks...", file=sys.stderr)
    
    for stock in WATCHLIST:
        symbol = stock["symbol"]
        name = stock["name"]
        target_up = stock.get("target_up")
        target_down = stock.get("target_down")
        
        try:
            # Get current price
            current_price = get_ltp(symbol)
            
            if current_price is None:
                print(f"  {symbol}: Could not fetch price", file=sys.stderr)
                continue
            
            print(f"  {symbol}: Price=₹{current_price:.2f}", file=sys.stderr)
            
            alert_triggered = False
            alert_msg = None
            
            # Check UPTARGET
            if target_up is not None and current_price >= target_up:
                alert_triggered = True
                alert_msg = format_alert_message(name, symbol, current_price, target_up, "UP")
            
            # Check DOWNTARGET (only if not already triggered for UP)
            elif target_down is not None and current_price <= target_down:
                alert_triggered = True
                alert_msg = format_alert_message(name, symbol, current_price, target_down, "DOWN")
            
            if alert_triggered:
                # Check if we've alerted recently (prevent spam)
                last_alert_time = alert_state.get(symbol, 0)
                current_time = time.time()
                
                # Only alert if last alert was more than 30 minutes ago
                if current_time - last_alert_time > 1800:  # 30 minutes
                    alert_messages.append(alert_msg)
                    alert_state[symbol] = current_time
                    print(f"  {symbol}: ALERT TRIGGERED", file=sys.stderr)
                else:
                    print(f"  {symbol}: Alert suppressed (recently alerted)", file=sys.stderr)
            else:
                print(f"  {symbol}: No alert triggered", file=sys.stderr)
                
        except Exception as e:
            print(f"  {symbol}: Error checking price - {e}", file=sys.stderr)
    
    # Save updated alert state
    save_alert_state(alert_state)
    
    return alert_messages

def main():
    """Main function to run price alert check."""
    try:
        alert_messages = check_price_alerts()
        for msg in alert_messages:
            # Print each alert message to stdout (cron job will capture this)
            print(msg)
        # If no alerts, print nothing to stdout
        return 0
    except Exception as e:
        error_msg = f"""<b>❌ Error in Price Alert System</b> – {datetime.now().strftime('%d %b %Y %H:%M IST')}
<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>

<b>Technical Details:</b> <code>{str(e)}</code>

<i>Please check the system logs for more information.</i>"""
        print(error_msg, file=sys.stderr)
        # Also print to stdout so cron job delivers the error?
        print(error_msg)
        return 1

if __name__ == '__main__':
    sys.exit(main())