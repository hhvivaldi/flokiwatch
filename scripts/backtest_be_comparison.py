"""
Breakeven Trigger Level Comparison Backtest
============================================
Runs 4 configurations to test different BE trigger levels:
- Baseline: 70% of SL (current production)
- Test A: 50% of SL
- Test B: Fixed 100 pips
- Test C: Fixed 150 pips

Period: Aug 18, 2025 → Feb 16, 2026 (6 months)
"""

import subprocess
import sys
import re
from datetime import datetime

# Configurations to test
CONFIGS = [
    {"name": "Baseline (70% SL)", "args": []},  # Uses default config
    {"name": "Test A (50% SL)", "args": ["--be-mult", "0.5"]},
    {"name": "Test B (100 pips)", "args": ["--be-fixed", "100"]},
    {"name": "Test C (150 pips)", "args": ["--be-fixed", "150"]},
]

def parse_results(output: str) -> dict:
    """Parse backtest output to extract key metrics."""
    results = {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "wr": 0.0,
        "pf": 0.0,
        "pnl": 0.0,
        "be_count": 0,
    }
    
    # Extract metrics using regex
    # Total trades
    m = re.search(r"Total trades:\s*(\d+)", output)
    if m:
        results["trades"] = int(m.group(1))
    
    # Wins/Losses
    m = re.search(r"Wins:\s*(\d+)", output)
    if m:
        results["wins"] = int(m.group(1))
    m = re.search(r"Losses:\s*(\d+)", output)
    if m:
        results["losses"] = int(m.group(1))
    
    # Win rate
    m = re.search(r"Win rate:\s*([\d.]+)%", output)
    if m:
        results["wr"] = float(m.group(1))
    
    # Profit factor
    m = re.search(r"Profit factor:\s*([\d.]+)", output)
    if m:
        results["pf"] = float(m.group(1))
    
    # Total P&L
    m = re.search(r"Total P&L:\s*\$?([-\d.]+)", output)
    if m:
        results["pnl"] = float(m.group(1))
    
    # Breakeven count (from close reasons)
    m = re.search(r"sl:\s*(\d+)", output)
    if m:
        sl_count = int(m.group(1))
    else:
        sl_count = 0
    m = re.search(r"tp:\s*(\d+)", output)
    if m:
        tp_count = int(m.group(1))
    else:
        tp_count = 0
    
    # BE activations can be inferred from trades that closed at entry price
    # For now, we'll track this differently
    
    return results


def run_backtest(config: dict) -> dict:
    """Run a single backtest configuration."""
    print(f"\n{'='*60}")
    print(f"Running: {config['name']}")
    print(f"{'='*60}")
    
    cmd = [
        sys.executable,
        "scripts/run_backtest.py",
    ] + config["args"]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
        )
        output = result.stdout + result.stderr
        
        # Print summary lines
        for line in output.split('\n'):
            if any(x in line for x in ['Total trades', 'Wins:', 'Losses:', 'Win rate', 'Profit factor', 'Total P&L']):
                print(f"  {line.strip()}")
        
        return parse_results(output)
    except subprocess.TimeoutExpired:
        print(f"  ERROR: Timeout after 10 minutes")
        return {"error": "timeout"}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"error": str(e)}


def main():
    print("=" * 70)
    print("  BREAKEVEN TRIGGER LEVEL COMPARISON BACKTEST")
    print("  Period: Aug 18, 2025 → Feb 16, 2026")
    print("=" * 70)
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = []
    
    for config in CONFIGS:
        result = run_backtest(config)
        result["name"] = config["name"]
        results.append(result)
    
    # Print comparison table
    print("\n")
    print("=" * 70)
    print("  COMPARISON RESULTS")
    print("=" * 70)
    print()
    print(f"{'Config':<25} {'Trades':<8} {'WR':<8} {'PF':<8} {'P&L':<12}")
    print("-" * 70)
    
    for r in results:
        if "error" in r:
            print(f"{r['name']:<25} ERROR: {r['error']}")
        else:
            print(f"{r['name']:<25} {r['trades']:<8} {r['wr']:<8.1f}% {r['pf']:<8.2f} ${r['pnl']:<12.2f}")
    
    print("-" * 70)
    
    # Save to file
    output_path = "data/be_backtest_comparison.txt"
    with open(output_path, 'w') as f:
        f.write("=" * 70 + "\n")
        f.write("BREAKEVEN TRIGGER LEVEL COMPARISON BACKTEST\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("Period: Aug 18, 2025 - Feb 16, 2026\n")
        f.write("=" * 70 + "\n\n")
        
        f.write(f"{'Config':<25} {'Trades':<8} {'WR':<8} {'PF':<8} {'P&L':<12}\n")
        f.write("-" * 70 + "\n")
        
        for r in results:
            if "error" in r:
                f.write(f"{r['name']:<25} ERROR: {r['error']}\n")
            else:
                f.write(f"{r['name']:<25} {r['trades']:<8} {r['wr']:<8.1f}% {r['pf']:<8.2f} ${r['pnl']:<12.2f}\n")
        
        f.write("-" * 70 + "\n")
        f.write("\nCONFIGURATIONS:\n")
        f.write("  Baseline: 70% of SL distance (current production)\n")
        f.write("  Test A: 50% of SL distance\n")
        f.write("  Test B: Fixed 100 pips\n")
        f.write("  Test C: Fixed 150 pips\n")
    
    print(f"\nResults saved to: {output_path}")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
