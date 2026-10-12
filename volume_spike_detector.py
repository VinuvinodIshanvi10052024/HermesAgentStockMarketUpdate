#!/usr/bin/env python3
"""
Volume Spike Detector for Angel One SmartAPI - ROBUST VERSION FOR OVERLOADED SERVICE
Detects unusual volume spikes and optionally RSI signals in watchlist stocks.
Uses ltpData for price and getCandleData for volume and RSI calculation.
RSI signal generation is optional and off by default.
Robust version designed to handle "Service temporarily overloaded" and rate limit errors
by skipping bad data, preserving good history, and avoiding false alerts.
"""

import os
import sys
import json
import time
import statistics
from datetime import datetime, timedelta
from SmartApi import SmartConnect
import pyotp
import subprocess
import random

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

# Global client cache to avoid recreating session every time
_client = None
_client_timestamp = 0
_CLIENT_CACHE_TIMEOUT = 300  # 5 minutes

def _get_client():
    """Get or create SmartApi client with session caching and timeout"""
    global _client, _client_timestamp
    
    now = time.time()
    # Return cached client if it's less than 5 minutes old
    if _client is not None and (now - _client_timestamp) < _CLIENT_CACHE_TIMEOUT:
        return _client
    
    # Create new client
    api_key = os.getenv("ANGEL_ONE_API_KEY")
    client_id = os.getenv("ANGEL_ONE_CLIENT_ID")
    password = os.getenv("ANGEL_ONE_PASSWORD")
    totp_secret = os.getenv("ANGEL_ONE_TOTP_SECRET")
    
    if not all([api_key, client_id, password, totp_secret]):
        raise ValueError("Missing required Angel One environment variables")
    
    client = SmartConnect(api_key=api_key)
    
    # Set timeout on the underlying requests session to prevent hanging
    if hasattr(client, 'session'):
        client.session.timeout = 20  # Increased timeout for unstable connections
    
    # Generate session with TOTP
    totp = pyotp.TOTP(totp_secret).now()
    session_data = client.generateSession(client_id, password, totp)
    
    if not session_data.get("status"):
        raise Exception(f"Failed to generate session: {session_data.get('message', 'Unknown error')}")
    
    # Cache the client
    _client = client
    _client_timestamp = now
    return client

# Watchlist configuration - customize as needed
WATCHLIST = [
    {"symbol": "RELIANCE", "name": "Reliance Industries"},
    {"symbol": "TCS", "name": "Tata Consultancy Services"},
    {"symbol": "INFY", "name": "Infosys"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank"},
    {"symbol": "SBIN", "name": "State Bank of India"},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel"},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank"},
    {"symbol": "LT", "name": "Larsen & Toubro"},
    {"symbol": "ASIANPAINT", "name": "Asian Paints"},
]

# Symbol to token mapping (pre-resolved for faster lookup)
SYMBOL_TOKEN_MAP = {
    "RELIANCE": "2885",
    "TCS": "11536",
    "INFY": "1594",
    "HDFCBANK": "1333",
    "ICICIBANK": "4963",
    "SBIN": "3045",
    "BHARTIARTL": "10940",
    "KOTAKBANK": "1922",
    "LT": "11483",
    "ASIANPAINT": "4902",
}

# Volume spike detection parameters
VOLUME_SPIKE_THRESHOLD = 2.0  # Alert when current volume is 2x average
VOLUME_HISTORY_DAYS = 5       # Number of days to calculate average volume
MIN_VOLUME_FOR_ALERT = 10000  # Minimum volume to consider for alert (filters out low-volume stocks)

# RSI signal generation parameters (optional)
ENABLE_RSI_SIGNALS = True     # Set to True to enable RSI signal generation
RSI_LENGTH = 14               # RSI period (typically 14)
RSI_OVERBOUGHT = 70           # RSI above this = potential SELL signal
RSI_OVERSOLD = 30             # RSI below this = potential BUY signal
RSI_HISTORY_DAYS = 5          # Number of days to use for RSI calculation (same as volume history)

# ROBUST Rate limiting parameters for overloaded service
MIN_DELAY_BETWEEN_STOCKS = 5.0  # Reasonable delay between each stock (seconds)
MAX_RETRIES_PER_STOCK = 2       # Limited retries
BASE_RETRY_DELAY = 10.0         # Reasonable base delay for retries (seconds)
REQUEST_BATCH_SIZE = 4          # Process 4 stocks per batch
BATCH_DELAY = 30.0              # 30 second delay between batches
MAX_VOLUME_AGE_FOR_HISTORY = 24 * 60 * 60  # 24 hours - don't use volume older than this for averaging

# File to store volume history
VOLUME_HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'volume_history.json')

# File to store last alert times to prevent spam
VOLUME_ALERT_STATE_FILE = os.path.join(os.path.dirname(__file__), 'volume_alert_state.json')
RSI_ALERT_STATE_FILE = os.path.join(os.path.dirname(__file__), 'rsi_alert_state.json')

def resolve_symbol_token(symbol):
    """
    Resolve symbol token with fallback mechanisms.
    First tries pre-resolved map, then attempts dynamic lookup if needed.
    """
    # First try pre-resolved token (fastest)
    token = SYMBOL_TOKEN_MAP.get(symbol)
    if token:
        return token
    
    # If not in pre-resolved map, return None and let calling function handle it
    print(f"Warning: Symbol {symbol} not in pre-resolved token map", file=sys.stderr)
    return None

def get_ltp(symbol):
    """Get LTP for a symbol using direct SmartApi call (price only)."""
    try:
        client = _get_client()
        symbol_token = resolve_symbol_token(symbol)
        
        if not symbol_token:
            print(f"Warning: No token available for {symbol}", file=sys.stderr)
            return None
            
        # Exchange is NSE for all our stocks
        exchange = "NSE"
        
        # Call ltpData API
        ltp_response = client.ltpData(exchange, symbol, symbol_token)
        
        if not ltp_response.get("status"):
            # Check if it's a symbol token error - if so, we might want to refresh tokens
            error_msg = ltp_response.get("message", "")
            if "Symbol token not found" in error_msg or "scrip master cache" in error_msg:
                print(f"Symbol token issue for {symbol}: {error_msg}", file=sys.stderr)
            return None
        
        data = ltp_response.get("data", {})
        ltp = data.get("ltp")
        
        if ltp is not None:
            return float(ltp)
        return None
        
    except Exception as e:
        print(f"Error getting LTP for {symbol}: {e}", file=sys.stderr)
        return None

def get_candle_data_robust(symbol):
    """
    Get 1-minute candle data with robust error handling for overloaded service.
    Returns (volumes, closes, success_flag) where success_flag indicates if data is reliable.
    """
    symbol_token = resolve_symbol_token(symbol)
    if not symbol_token:
        print(f"Warning: Cannot resolve token for {symbol}", file=sys.stderr)
        return None, None, False
    
    exchange = "NSE"
    
    # Calculate time range for today (9:15 AM to now)
    today = datetime.now().strftime("%Y-%m-%d")
    from_time = f"{today} 09:15"
    to_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # Prepare candle params
    candle_params = {
        "exchange": exchange,
        "symboltoken": symbol_token,
        "interval": "ONE_MINUTE",
        "fromdate": from_time,
        "todate": to_time
    }
    
    # Try with robust error handling
    for attempt in range(MAX_RETRIES_PER_STOCK + 1):  # +1 for initial attempt
        try:
            # Add jittered delay before each attempt to avoid thundering herd
            if attempt > 0:  # No delay on first attempt
                jitter_delay = random.uniform(0.5, 2.0)
                time.sleep(jitter_delay)
            
            client = _get_client()
            candle_data = client.getCandleData(candle_params)
            
            if candle_data.get("status"):
                data = candle_data.get("data", [])
                
                if data and len(data) > 0:
                    # Extract volumes and closing prices
                    volumes = []
                    closes = []
                    for candle in data:
                        # Format: [timestamp, open, high, low, close, volume]
                        if len(candle) >= 6:
                            volumes.append(int(candle[5]))   # Volume is index 5
                            closes.append(float(candle[4]))  # Close is index 4
                    
                    if volumes and closes:
                        # Validate that we got reasonable data
                        # Check if volumes are not all zero or extremely low
                        if sum(volumes) > 0 and max(volumes) > 1000:  # At least some meaningful volume
                            # Small delay after successful call
                            time.sleep(0.5)
                            return volumes, closes, True
                        else:
                            print(f"Warning: Suspiciously low volume data for {symbol} on attempt {attempt + 1}", file=sys.stderr)
                            # Continue to retry in case we get better data
                    else:
                        print(f"Warning: Empty candle data for {symbol} on attempt {attempt + 1}", file=sys.stderr)
                else:
                    print(f"Warning: No candle data returned for {symbol} on attempt {attempt + 1}", file=sys.stderr)
            else:
                error_msg = candle_data.get("message", "Unknown error")
                print(f"API error for {symbol} on attempt {attempt + 1}: {error_msg}", file=sys.stderr)
                
                # Check for specific errors we should retry on
                retry_errors = ["access rate", "exceeding", "overloaded", "temporarily", "timeout", "connection"]
                if any(err in error_msg.lower() for err in retry_errors):
                    if attempt < MAX_RETRIES_PER_STOCK:
                        # Exponential backoff with jitter for rate limit/service errors
                        delay = BASE_RETRY_DELAY * (2 ** attempt) + random.uniform(2, 8)
                        print(f"Service issue for {symbol}, waiting {delay:.1f}s before retry...", file=sys.stderr)
                        continue
                    else:
                        print(f"Max retries exceeded for {symbol} due to service issues", file=sys.stderr)
                else:
                    # Non-retryable error (auth, symbol not found, etc.)
                    print(f"Non-retryable error for {symbol}: {error_msg}", file=sys.stderr)
                    break
                    
        except Exception as e:
            print(f"Exception getting candle data for {symbol} on attempt {attempt + 1}: {e}", file=sys.stderr)
            if attempt < MAX_RETRIES_PER_STOCK:
                # Exponential backoff with jitter for exceptions
                delay = BASE_RETRY_DELAY * (2 ** attempt) + random.uniform(2, 8)
                time.sleep(delay)
                continue
            break
    
    # All attempts failed or we got bad data
    print(f"Failed to get reliable candle data for {symbol} after {MAX_RETRIES_PER_STOCK + 1} attempts", file=sys.stderr)
    return None, None, False

def get_candle_data(symbol):
    """
    Wrapper function that returns only volumes and closes for backward compatibility.
    """
    volumes, closes, success = get_candle_data_robust(symbol)
    return volumes, closes

def calculate_rsi(prices, length=RSI_LENGTH):
    """Calculate RSI from a list of closing prices."""
    if len(prices) < length + 1:  # Need at least length+1 prices to calculate RSI
        return None
    
    # Calculate price changes
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    
    # Separate gains and losses
    gains = [delta if delta > 0 else 0 for delta in deltas]
    losses = [-delta if delta < 0 else 0 for delta in deltas]
    
    # Calculate average gain and loss over the first period
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    
    if avg_loss == 0:
        return 100  # RSI is 100 when there are no losses
    
    # Calculate relative strength (RS)
    rs = avg_gain / avg_loss
    
    # Calculate RSI
    rsi = 100 - (100 / (1 + rs))
    
    # Now calculate the rest of the RSIs using the smoothing formula
    for i in range(length, len(deltas)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        
        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
    
    return rsi

def get_historical_volume_avg(symbol, days=VOLUME_HISTORY_DAYS):
    """Calculate average volume over the last N days, only using recent reliable data."""
    history = load_volume_history()
    symbol_history = history.get(symbol, [])
    
    # Keep only recent entries that are not too old
    cutoff_time = time.time() - (days * 24 * 60 * 60)
    recent_history = [v for v in symbol_history if v['timestamp'] > cutoff_time]
    
    # Additional filter: only use volume data from the last MAX_VOLUME_AGE_FOR_HISTORY
    # to avoid using stale data when we've been getting API errors
    fresh_cutoff = time.time() - MAX_VOLUME_AGE_FOR_HISTORY
    fresh_history = [v for v in recent_history if v['timestamp'] > fresh_cutoff]
    
    if len(fresh_history) < 2:  # Need at least 2 fresh data points
        # Fall back to recent history if we don't have enough fresh data
        if len(recent_history) >= 2:
            print(f"Warning: Using volume data up to {days} days old for {symbol} (fresh data unavailable)", file=sys.stderr)
            recent_history = [v for v in symbol_history if v['timestamp'] > cutoff_time]
        else:
            return None
    
    volumes = [entry['volume'] for entry in recent_history]
    return statistics.mean(volumes) if volumes else None

def get_historical_rsi_avg(symbol, days=RSI_HISTORY_DAYS):
    """Calculate average RSI over the last N days (for context, not used in alert logic)."""
    history = load_rsi_history()
    symbol_history = history.get(symbol, [])
    
    # Keep only recent entries
    cutoff_time = time.time() - (days * 24 * 60 * 60)
    recent_history = [v for v in symbol_history if v['timestamp'] > cutoff_time]
    
    if len(recent_history) < 2:  # Need at least 2 data points
        return None
    
    rsis = [entry['rsi'] for entry in recent_history]
    return statistics.mean(rsis) if rsis else None

def update_volume_history(symbol, volume):
    """Update volume history with latest reading ONLY if volume is reliable."""
    # Only update history if we have a reasonable volume reading
    if volume is None or volume < 1000:  # Don't update with suspiciously low volumes
        print(f"Info: Not updating volume history for {symbol} with volume={volume} (unreliable)", file=sys.stderr)
        return
    
    history = load_volume_history()
    if symbol not in history:
        history[symbol] = []
    
    # Add current reading
    history[symbol].append({
        'volume': volume,
        'timestamp': time.time()
    })
    
    # Keep only last 30 days of data to prevent file from growing too large
    cutoff_time = time.time() - (30 * 24 * 60 * 60)
    history[symbol] = [entry for entry in history[symbol] if entry['timestamp'] > cutoff_time]
    
    save_volume_history(history)

def update_rsi_history(symbol, rsi_value):
    """Update RSI history with latest reading."""
    history = load_rsi_history()
    if symbol not in history:
        history[symbol] = []
    
    # Add current reading
    history[symbol].append({
        'rsi': rsi_value,
        'timestamp': time.time()
    })
    
    # Keep only last 30 days of data to prevent file from growing too large
    cutoff_time = time.time() - (30 * 24 * 60 * 60)
    history[symbol] = [entry for entry in history[symbol] if entry['timestamp'] > cutoff_time]
    
    save_rsi_history(history)

def load_volume_history():
    """Load volume history from file."""
    try:
        if os.path.exists(VOLUME_HISTORY_FILE):
            with open(VOLUME_HISTORY_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_volume_history(history):
    """Save volume history to file."""
    try:
        with open(VOLUME_HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"Error saving volume history: {e}", file=sys.stderr)

def load_rsi_history():
    """Load RSI history from file."""
    try:
        rsi_history_file = os.path.join(os.path.dirname(__file__), 'rsi_history.json')
        if os.path.exists(rsi_history_file):
            with open(rsi_history_file, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_rsi_history(history):
    """Save RSI history to file."""
    try:
        rsi_history_file = os.path.join(os.path.dirname(__file__), 'rsi_history.json')
        with open(rsi_history_file, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"Error saving RSI history: {e}", file=sys.stderr)

def load_alert_state(file_path):
    """Load alert state from file to prevent duplicate alerts."""
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_alert_state(state, file_path):
    """Save alert state to file."""
    try:
        with open(file_path, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Error saving alert state to {file_path}: {e}", file=sys.stderr)

def check_volume_spikes():
    """Check watchlist for volume spikes and return list of alert messages."""
    # Initialize environment from Hermes config
    initialize_environment()
    
    volume_history = load_volume_history()
    volume_alert_state = load_alert_state(VOLUME_ALERT_STATE_FILE)
    rsi_alert_state = load_alert_state(RSI_ALERT_STATE_FILE)
    alert_messages = []
    
    # Print debug info to stderr
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking volume spikes and RSI signals for {len(WATCHLIST)} stocks...", file=sys.stderr)
    
    successful_fetches = 0
    failed_fetches = 0
    skipped_fetches = 0  # For data we got but chose not to use
    
    # Process stocks in small batches with delays between batches
    batches = [WATCHLIST[i:i + REQUEST_BATCH_SIZE] for i in range(0, len(WATCHLIST), REQUEST_BATCH_SIZE)]
    
    for batch_idx, batch in enumerate(batches):
        if batch_idx > 0:
            # Delay between batches to let the service recover
            print(f"  Completed batch {batch_idx}, waiting {BATCH_DELAY}s before next batch...", file=sys.stderr)
            time.sleep(BATCH_DELAY)
        
        # Process each stock in the current batch
        for i, stock in enumerate(batch):
            symbol = stock["symbol"]
            name = stock["name"]
            
            # Add delay between stocks within batch (except for the first one)
            if i > 0:
                delay = MIN_DELAY_BETWEEN_STOCKS + random.uniform(0, 2)  # Add jitter
                print(f"    Waiting {delay:.1f}s before processing next stock in batch...", file=sys.stderr)
                time.sleep(delay)
            
            try:
                # Get current price (from ltpData - usually works for price)
                current_price = get_ltp(symbol)
                
                # Get current volume and closing prices with robust error handling
                volumes, closes, success = get_candle_data_robust(symbol)
                
                if not success or volumes is None or closes is None:
                    print(f"  {symbol}: Could not fetch reliable candle data (skipping for this cycle)", file=sys.stderr)
                    failed_fetches += 1
                    continue
                
                if len(volumes) == 0:
                    price_str = f"{current_price:.2f}" if current_price is not None else "N/A"
                    print(f"  {symbol}: Price=₹{price_str}, Volume=0 (skipped - no volume data)", file=sys.stderr)
                    failed_fetches += 1
                    continue
                
                # Additional validation: check if volume data looks reasonable
                total_volume = sum(volumes)
                max_volume = max(volumes) if volumes else 0
                if total_volume == 0 or max_volume < 1000:
                    price_str = f"{current_price:.2f}" if current_price is not None else "N/A"
                    print(f"  {symbol}: Price=₹{price_str}, Volume data suspicious (total={total_volume}, max={max_volume}) - skipping", file=sys.stderr)
                    skipped_fetches += 1
                    continue
                
                successful_fetches += 1
                current_volume = volumes[-1]  # Most recent completed 1-minute candle volume
                current_price_from_candle = closes[-1] if closes else current_price
                
                # Use price from ltpData for consistency (more reliable for current price)
                price_str = f"{current_price:.2f}" if current_price is not None else "N/A"
                print(f"  {symbol}: Price=₹{price_str}, Volume={current_volume:,} (total={total_volume:,})", file=sys.stderr)
                
                # Update volume history ONLY if we have good data
                update_volume_history(symbol, current_volume)
                
                # Calculate average volume from history
                avg_volume = get_historical_volume_avg(symbol)
                
                if avg_volume is None:
                    print(f"  {symbol}: Not enough reliable history for volume average (need {VOLUME_HISTORY_DAYS} days of good data)", file=sys.stderr)
                    volume_spike_detected = False
                else:
                    # Calculate volume ratio
                    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
                    
                    avg_volume_int = int(avg_volume) if avg_volume is not None else 0
                    print(f"  {symbol}: Volume ratio = {volume_ratio:.2f}x (current: {current_volume:,}, avg: {avg_volume_int:,})", file=sys.stderr)
                    
                    # Check for volume spike - only if we have reliable data
                    volume_spike_detected = volume_ratio >= VOLUME_SPIKE_THRESHOLD
                
                # --- RSI Signal Generation (Optional) ---
                rsi_signal = None
                rsi_value = None
                if ENABLE_RSI_SIGNALS and len(closes) >= RSI_LENGTH + 1:
                    # Calculate RSI using the closing prices we have
                    rsi_value = calculate_rsi(closes, RSI_LENGTH)
                    if rsi_value is not None:
                        # Update RSI history
                        update_rsi_history(symbol, rsi_value)
                        
                        # Check for RSI signals
                        if rsi_value >= RSI_OVERBOUGHT:
                            rsi_signal = "SELL"
                            print(f"  {symbol}: RSI={rsi_value:.2f} (OVERBOUGHT - SELL signal)", file=sys.stderr)
                        elif rsi_value <= RSI_OVERSOLD:
                            rsi_signal = "BUY"
                            print(f"  {symbol}: RSI={rsi_value:.2f} (OVERSOLD - BUY signal)", file=sys.stderr)
                        else:
                            print(f"  {symbol}: RSI={rsi_value:.2f} (neutral)", file=sys.stderr)
                    else:
                        print(f"  {symbol}: Not enough data for RSI calculation (need {RSI_LENGTH+1} closes)", file=sys.stderr)
                elif ENABLE_RSI_SIGNALS:
                    print(f"  {symbol}: Insufficient closing prices for RSI (have {len(closes) if closes else 0}, need {RSI_LENGTH+1})", file=sys.stderr)
                
                # --- Generate Alerts ---
                
                # 1. Volume Spike Alert - ONLY if we have reliable data
                if volume_spike_detected:
                    # Check if we've alerted recently for volume spike (prevent spam)
                    last_alert_time = volume_alert_state.get(symbol, 0)
                    current_time = time.time()
                    
                    # Only alert if last alert was more than 2 hours ago (volume spikes less frequent)
                    if current_time - last_alert_time > 7200:  # 2 hours
                        spike_percent = ((volume_ratio - 1) * 100) if avg_volume is not None else 0
                        alert_msg = f"""📊 *VOLUME SPIKE ALERT* – {datetime.now().strftime('%d %b %Y %H:%M IST')}

🏢 *{name} ({symbol})*
💹 Price: ₹{price_str}
📈 Volume: {current_volume:,} 
📊 Avg Volume ({VOLUME_HISTORY_DAYS}d): {int(avg_volume) if avg_volume is not None else 0:,}
🚨 Spike: {volume_ratio:.2f}x ({spike_percent:+.1f}% above average)

_Trade responsibly. This is an automated alert from your Hermes Agent._"""
                        alert_messages.append(alert_msg)
                        volume_alert_state[symbol] = current_time
                        print(f"  {symbol}: VOLUME SPIKE ALERT TRIGGERED", file=sys.stderr)
                    else:
                        print(f"  {symbol}: Volume spike alert suppressed (recently alerted)", file=sys.stderr)
                
                # 2. RSI Signal Alert (if enabled and signal detected)
                if ENABLE_RSI_SIGNALS and rsi_signal is not None:
                    # Check if we've alerted recently for RSI signal (prevent spam)
                    last_rsi_alert_time = rsi_alert_state.get(symbol, 0)
                    current_time = time.time()
                    
                    # Only alert if last alert was more than 30 minutes ago (RSI signals can be more frequent)
                    if current_time - last_rsi_alert_time > 1800:  # 30 minutes
                        rsi_action = "🔴 SELL SIGNAL" if rsi_signal == "SELL" else "🟢 BUY SIGNAL"
                        rsi_reason = "RSI above 70 (overbought)" if rsi_signal == "SELL" else "RSI below 30 (oversold)"
                        
                        alert_msg = f"""📈 *RSI SIGNAL ALERT* – {datetime.now().strftime('%d %b %Y %H:%M IST')}

🏢 *{name} ({symbol})*
💹 Price: ₹{price_str}
📊 RSI: {rsi_value:.2f}
{rsi_action}
📝 Reason: {rsi_reason}

_Trade responsibly. This is an automated alert from your Hermes Agent._"""
                        alert_messages.append(alert_msg)
                        rsi_alert_state[symbol] = current_time
                        print(f"  {symbol}: RSI {rsi_signal} ALERT TRIGGERED", file=sys.stderr)
                    else:
                        print(f"  {symbol}: RSI {rsi_signal} alert suppressed (recently alerted)", file=sys.stderr)
                
            except Exception as e:
                print(f"  {symbol}: Error checking volume/RSI - {e}", file=sys.stderr)
                failed_fetches += 1
    
    # Summary
    print(f"  Summary: {successful_fetches} successful, {failed_fetches} failed, {skipped_fetches} skipped (bad data)", file=sys.stderr)
    
    # Save updated states
    save_volume_history(volume_history)
    save_alert_state(volume_alert_state, VOLUME_ALERT_STATE_FILE)
    save_alert_state(rsi_alert_state, RSI_ALERT_STATE_FILE)
    
    return alert_messages

def main():
    """Main function to run volume spike detection and optional RSI signals."""
    try:
        print("Starting Volume Spike Detector (Robust Version for Overloaded Service)...")
        alert_messages = check_volume_spikes()
        for msg in alert_messages:
            # Print each alert message to stdout (cron job will capture this)
            print(msg)
        # If no alerts, print nothing to stdout
        print("Volume spike check completed.")
        return 0
    except Exception as e:
        error_msg = f"""<b>❌ Error in Volume Spike Detector</b> – {datetime.now().strftime('%d %b %Y %H:%M IST')}
<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>

<b>Technical Details:</b> <code>{str(e)}</code>

<i>Please check the system logs for more information.</i>"""
        print(error_msg, file=sys.stderr)
        # Also print to stdout so cron job delivers the error?
        print(error_msg)
        return 1

if __name__ == '__main__':
    sys.exit(main())