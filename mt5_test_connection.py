"""
Test script - Python connection with MetaTrader 5
Project: Trading Bot XAU/USD
Step 1: Validate connection and get basic data
"""

import MetaTrader5 as mt5
from datetime import datetime

# Account credentials
ACCOUNT = 52704729
PASSWORD = "EnK2S8TUd&l$VG"
SERVER = "CapitalPointTrading-Demo"

def main():
    print("=" * 50)
    print("MT5 CONNECTION TEST - XAU/USD Trading Bot")
    print("=" * 50)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1. Initialize MT5
    print("[1] Initializing MT5 connection...")
    if not mt5.initialize():
        print(f"❌ Failed to initialize MT5: {mt5.last_error()}")
        return
    print("✅ MT5 initialized successfully!")
    
    # Show MT5 version
    terminal_info = mt5.terminal_info()
    if terminal_info:
        print(f"   Terminal: {terminal_info.name}")
        print(f"   Build: {terminal_info.build}")
    print()

    # 2. Account login
    print(f"[2] Logging into account {ACCOUNT}...")
    if not mt5.login(ACCOUNT, password=PASSWORD, server=SERVER):
        print(f"❌ Login failed: {mt5.last_error()}")
        mt5.shutdown()
        return
    print("✅ Login successful!")
    print()

    # 3. Account information
    print("[3] Account information:")
    account_info = mt5.account_info()
    if account_info:
        print(f"   Name: {account_info.name}")
        print(f"   Server: {account_info.server}")
        print(f"   Currency: {account_info.currency}")
        print(f"   Balance: {account_info.balance:.2f} {account_info.currency}")
        print(f"   Equity: {account_info.equity:.2f} {account_info.currency}")
        print(f"   Free margin: {account_info.margin_free:.2f} {account_info.currency}")
        print(f"   Leverage: 1:{account_info.leverage}")
    else:
        print(f"❌ Error getting account info: {mt5.last_error()}")
    print()

    # 4. Current XAU/USD price
    print("[4] XAU/USD quote:")
    symbol = "XAUUSD"
    
    # Check if symbol exists
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        # Try common symbol variations
        for alt_symbol in ["GOLD", "XAUUSDm", "XAUUSD.a", "XAUUSD#"]:
            symbol_info = mt5.symbol_info(alt_symbol)
            if symbol_info:
                symbol = alt_symbol
                break
    
    if symbol_info is None:
        print(f"❌ Symbol {symbol} not found")
        # List available symbols with 'XAU' or 'GOLD'
        print("   Searching for related symbols...")
        all_symbols = mt5.symbols_get()
        gold_symbols = [s.name for s in all_symbols if 'XAU' in s.name.upper() or 'GOLD' in s.name.upper()]
        if gold_symbols:
            print(f"   Symbols found: {gold_symbols[:5]}")
    else:
        # Ensure symbol is visible in Market Watch
        if not symbol_info.visible:
            mt5.symbol_select(symbol, True)
        
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            print(f"   Symbol: {symbol}")
            print(f"   Bid: {tick.bid}")
            print(f"   Ask: {tick.ask}")
            print(f"   Spread: {(tick.ask - tick.bid):.2f}")
            print(f"   Last tick: {datetime.fromtimestamp(tick.time)}")
        else:
            print(f"❌ Error getting tick: {mt5.last_error()}")
    print()

    # 5. Disconnect
    print("[5] Disconnecting from MT5...")
    mt5.shutdown()
    print("✅ Disconnected successfully!")
    print()
    print("=" * 50)
    print("TEST COMPLETED!")
    print("=" * 50)

if __name__ == "__main__":
    main()
