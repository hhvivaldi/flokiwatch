//+------------------------------------------------------------------+
//|                                                  FlokiBridge.mq5 |
//|                                        FlokiWatch Trading System |
//|                           Execution Bridge for Python Brain      |
//+------------------------------------------------------------------+
#property copyright "FlokiWatch"
#property link      ""
#property version   "1.02"
#property strict

//+------------------------------------------------------------------+
//| Input Parameters                                                  |
//+------------------------------------------------------------------+
input string   SignalFile = "brain_signal.json";      // Signal file from Python
input string   StatusFile = "ea_status.json";         // Status file for Python
input int      MagicNumber = 234000;                  // Magic number for orders
input int      MaxSlippage = 20;                      // Max slippage in points
input int      StatusUpdateMs = 1000;                 // Status update interval (ms)
input bool     EnableLogging = true;                  // Enable detailed logging

//+------------------------------------------------------------------+
//| Global Variables                                                  |
//+------------------------------------------------------------------+
string g_lastSignalId = "";                           // Last processed signal ID
datetime g_lastFileCheck = 0;                         // Last file modification time
bool g_statusDirty = true;                            // Flag to write status

// Diagnostic tracking for failure analysis
long g_heartbeatCount = 0;                            // Increments every OnTimer() call
datetime g_lastHeartbeatTime = 0;                     // Last successful OnTimer() execution
datetime g_lastStatusWriteTime = 0;                   // Last successful status file write (for OnTick backup)
int g_consecutiveWriteFailures = 0;                   // Consecutive status file write failures
string g_lastWriteError = "";                         // Last write error message
bool g_timerStarted = false;                          // Whether EventSetMillisecondTimer succeeded

// Position tracking
struct PositionData
{
   ulong    ticket;
   string   direction;
   double   volume;
   double   openPrice;
   double   currentSL;
   double   currentTP;
   datetime openTime;
   bool     breakevenHit;
   bool     trailingActive;
   double   maxProfitPips;
   double   breakevenTrigger;
   double   trailingTrigger;
   double   trailingDistance;
   double   maxDrawdownPips;
};

PositionData g_positions[];                           // Tracked positions
int g_positionCount = 0;

// Signal data
struct SignalData
{
   int      version;
   string   timestamp;
   string   signalId;
   string   signal;
   double   sl;
   double   tp;
   double   lotSize;
   double   confidence;
   int      magic;
   string   comment;
   double   breakevenTriggerPips;
   double   trailingTriggerPips;
   double   trailingDistancePips;
   double   maxDrawdownPips;
};

// Closed trades today
struct ClosedTrade
{
   ulong    ticket;
   string   direction;
   double   volume;
   double   openPrice;
   double   closePrice;
   double   profit;
   string   closeReason;
   datetime closeTime;
};

ClosedTrade g_closedToday[];
int g_closedCount = 0;

// Last error
string g_lastError = "";

// ── FLO-231: Price Alert Monitoring ──
string   PriceAlertFile        = "price_alerts.json";
string   PriceAlertTriggerFile = "price_alert_triggered.json";
datetime g_lastAlertFileCheck  = 0;
int      g_alertCheckInterval  = 5;  // re-read file every 5 seconds
string   ALERT_LINE_PREFIX     = "FLOKI_ALERT_";

struct PriceAlert {
   string   id;
   string   alertType;  // "price_above" or "price_below"
   double   level;
   bool     triggered;
   datetime touchTime;
   double   touchPrice;
};

PriceAlert g_alerts[];
int        g_alertCount = 0;
int        g_lastAlertVersion = -1;

// ── Chart Screenshot Capture ──
string   ScreenshotRequestFile = "screenshot_request.json";
string   ScreenshotReadyFile   = "screenshot_ready.json";
string   ScreenshotH1File      = "chart_h1.png";
string   ScreenshotM15File     = "chart_m15.png";

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit()
{
   // Set timer for status updates (check return value)
   g_timerStarted = EventSetMillisecondTimer(StatusUpdateMs);
   
   // Initialize arrays
   ArrayResize(g_positions, 0);
   ArrayResize(g_closedToday, 0);
   g_positionCount = 0;
   g_closedCount = 0;
   
   // Initialize diagnostic counters
   g_heartbeatCount = 0;
   g_lastHeartbeatTime = TimeCurrent();
   g_lastStatusWriteTime = TimeCurrent();
   g_consecutiveWriteFailures = 0;
   g_lastWriteError = "";
   
   // Scan existing positions
   ScanExistingPositions();
   
   // Startup diagnostic log
   Print("=== FlokiBridge STARTUP ===");
   Print("  Version: 1.02");
   Print("  Magic: ", MagicNumber);
   Print("  Symbol: ", _Symbol);
   Print("  Timer interval: ", StatusUpdateMs, "ms");
   Print("  Timer started: ", g_timerStarted ? "YES" : "NO (OnTick backup active)");
   Print("  Signal file: ", SignalFile);
   Print("  Status file: ", StatusFile);
   Print("  Positions found: ", g_positionCount);
   Print("  Logging enabled: ", EnableLogging);
   Print("===========================");
   
   if(!g_timerStarted)
      Print("WARNING: Timer failed to start! OnTick() will handle heartbeats.");
   
   // Write initial status
   g_statusDirty = true;
   WriteStatus();
   
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   RemoveAlertLines();
   if(EnableLogging)
      Print("FlokiBridge stopped. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| Timer function - writes status periodically                       |
//+------------------------------------------------------------------+
void OnTimer()
{
   // Increment heartbeat counter FIRST (proves OnTimer fired)
   g_heartbeatCount++;
   g_lastHeartbeatTime = TimeCurrent();
   
   // Wrap all logic in error handling
   bool timerSuccess = false;
   
   // Update position data
   UpdatePositionData();
   
   // Write status if dirty or periodically (always write to keep heartbeat fresh)
   bool writeOk = WriteStatus();
   
   if(writeOk)
   {
      g_consecutiveWriteFailures = 0;
      g_lastWriteError = "";
      g_statusDirty = false;
      g_lastStatusWriteTime = TimeCurrent();
      timerSuccess = true;
   }
   else
   {
      g_consecutiveWriteFailures++;
      
      // Log warning if failures are accumulating
      if(g_consecutiveWriteFailures == 5)
         Print("WARNING: 5 consecutive status write failures. Last error: ", g_lastWriteError);
      else if(g_consecutiveWriteFailures == 20)
         Print("CRITICAL: 20 consecutive status write failures. EA may appear offline to Python.");
      else if(g_consecutiveWriteFailures % 100 == 0)
         Print("ALERT: ", g_consecutiveWriteFailures, " consecutive write failures. Error: ", g_lastWriteError);
   }
   
   // Check for screenshot requests from Python
   CheckScreenshotRequest();

   // Periodic heartbeat log (every 60 seconds = ~60 timer calls at 1000ms)
   if(EnableLogging && g_heartbeatCount % 60 == 0)
      Print("Heartbeat #", g_heartbeatCount, " | Positions: ", g_positionCount,
            " | Write failures: ", g_consecutiveWriteFailures);
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick()
{
   // Check for new signal (only if file changed)
   CheckSignalFile();

   // FLO-231: Tick-level price alert monitoring
   CheckPriceAlerts();

   // Manage open positions (breakeven, trailing, drawdown)
   ManagePositions();
   
   // Backup heartbeat: if OnTimer() isn't firing, write status from OnTick()
   // This handles cases where EventSetMillisecondTimer() failed or Algo Trading is disabled
   if(TimeCurrent() - g_lastStatusWriteTime > 30)
   {
      UpdatePositionData();
      bool writeOk = WriteStatus();
      if(writeOk)
      {
         g_lastStatusWriteTime = TimeCurrent();
         g_consecutiveWriteFailures = 0;
         
         // Log that backup heartbeat is active (only once per minute to avoid spam)
         static datetime lastBackupLog = 0;
         if(TimeCurrent() - lastBackupLog > 60)
         {
            Print("OnTick backup heartbeat active (OnTimer not firing)");
            lastBackupLog = TimeCurrent();
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Scan existing positions at startup                                |
//+------------------------------------------------------------------+
void ScanExistingPositions()
{
   ArrayResize(g_positions, 0);
   g_positionCount = 0;
   
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      
      // Add to tracking
      ArrayResize(g_positions, g_positionCount + 1);
      
      g_positions[g_positionCount].ticket = ticket;
      g_positions[g_positionCount].direction = 
         (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      g_positions[g_positionCount].volume = PositionGetDouble(POSITION_VOLUME);
      g_positions[g_positionCount].openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      g_positions[g_positionCount].currentSL = PositionGetDouble(POSITION_SL);
      g_positions[g_positionCount].currentTP = PositionGetDouble(POSITION_TP);
      g_positions[g_positionCount].openTime = (datetime)PositionGetInteger(POSITION_TIME);
      g_positions[g_positionCount].breakevenHit = false;
      g_positions[g_positionCount].trailingActive = false;
      g_positions[g_positionCount].maxProfitPips = 0;
      
      // Default trailing params (will be updated from signal)
      g_positions[g_positionCount].breakevenTrigger = 100;
      g_positions[g_positionCount].trailingTrigger = 150;
      g_positions[g_positionCount].trailingDistance = 100;
      g_positions[g_positionCount].maxDrawdownPips = 1000;
      
      // Check if already at breakeven (SL near entry)
      double slDist = MathAbs(g_positions[g_positionCount].openPrice - 
                              g_positions[g_positionCount].currentSL);
      if(slDist < 5 * _Point * 10) // Within 5 pips of entry
         g_positions[g_positionCount].breakevenHit = true;
      
      g_positionCount++;
      
      if(EnableLogging)
         Print("Scanned position #", ticket, " ", 
               g_positions[g_positionCount-1].direction);
   }
}

//+------------------------------------------------------------------+
//| Check signal file for new signals                                 |
//+------------------------------------------------------------------+
void CheckSignalFile()
{
   // Check if file exists (terminal-specific MQL5\Files folder)
   if(!FileIsExist(SignalFile))
      return;
   
   // Check file modification time
   datetime fileTime = (datetime)FileGetInteger(SignalFile, FILE_MODIFY_DATE);
   if(fileTime == g_lastFileCheck)
      return;
   
   g_lastFileCheck = fileTime;
   
   // Read and parse signal
   SignalData signal;
   if(!ReadSignalFile(signal))
      return;
   
   // Check if already processed
   if(signal.signalId == g_lastSignalId)
      return;
   
   // Check if signal is too old (>10 minutes)
   datetime signalTime = StringToTime(signal.timestamp);
   if(TimeCurrent() - signalTime > 600)
   {
      if(EnableLogging)
         Print("Signal too old, ignoring: ", signal.signalId);
      g_lastSignalId = signal.signalId;
      return;
   }
   
   // Process signal
   ProcessSignal(signal);
   
   g_lastSignalId = signal.signalId;
   g_statusDirty = true;
}

//+------------------------------------------------------------------+
//| Read and parse signal JSON file                                   |
//+------------------------------------------------------------------+
bool ReadSignalFile(SignalData &signal)
{
   int handle = FileOpen(SignalFile, FILE_READ|FILE_TXT|FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      g_lastError = "Cannot open signal file";
      return false;
   }
   
   string content = "";
   while(!FileIsEnding(handle))
   {
      content += FileReadString(handle);
   }
   FileClose(handle);
   
   // Parse JSON manually (MQL5 doesn't have native JSON)
   signal.version = (int)GetJsonInt(content, "version");
   signal.timestamp = GetJsonString(content, "timestamp");
   signal.signalId = GetJsonString(content, "signal_id");
   signal.signal = GetJsonString(content, "signal");
   signal.sl = GetJsonDouble(content, "sl");
   signal.tp = GetJsonDouble(content, "tp");
   signal.lotSize = GetJsonDouble(content, "lot_size");
   signal.confidence = GetJsonDouble(content, "confidence");
   signal.magic = (int)GetJsonInt(content, "magic");
   signal.comment = GetJsonString(content, "comment");
   signal.breakevenTriggerPips = GetJsonDouble(content, "breakeven_trigger_pips");
   signal.trailingTriggerPips = GetJsonDouble(content, "trailing_trigger_pips");
   signal.trailingDistancePips = GetJsonDouble(content, "trailing_distance_pips");
   signal.maxDrawdownPips = GetJsonDouble(content, "max_drawdown_pips");
   
   if(signal.signalId == "")
   {
      g_lastError = "Invalid signal file format";
      return false;
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Process a signal                                                  |
//+------------------------------------------------------------------+
void ProcessSignal(SignalData &signal)
{
   if(EnableLogging)
      Print("Processing signal: ", signal.signalId, " | ", signal.signal,
            " | SL:", signal.sl, " TP:", signal.tp, " Lot:", signal.lotSize);
   
   if(signal.signal == "BUY")
   {
      ExecuteBuy(signal);
   }
   else if(signal.signal == "SELL")
   {
      ExecuteSell(signal);
   }
   else if(signal.signal == "CLOSE")
   {
      CloseAllPositions("Signal");
   }
   // HOLD = do nothing
   
   // Write status immediately after processing to capture any errors
   WriteStatus();
   
   // Log the result
   if(EnableLogging)
   {
      if(g_lastError == "")
         Print("Signal processed successfully: ", signal.signalId);
      else
         Print("Signal processing FAILED: ", signal.signalId, " | Error: ", g_lastError);
   }
}

//+------------------------------------------------------------------+
//| Execute BUY order                                                 |
//+------------------------------------------------------------------+
void ExecuteBuy(SignalData &signal)
{
   MqlTradeRequest request = {};
   MqlTradeResult result = {};
   
   request.action = TRADE_ACTION_DEAL;
   request.symbol = _Symbol;
   request.volume = signal.lotSize;
   request.type = ORDER_TYPE_BUY;
   request.price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   request.sl = signal.sl;
   request.tp = signal.tp;
   request.deviation = MaxSlippage;
   request.magic = MagicNumber;
   request.comment = signal.comment;
   request.type_filling = ORDER_FILLING_IOC;
   request.type_time = ORDER_TIME_GTC;
   
   if(!OrderSend(request, result))
   {
      g_lastError = "BUY failed: " + IntegerToString(result.retcode);
      if(EnableLogging)
         Print("BUY order failed: ", result.retcode, " - ", GetRetcodeDescription(result.retcode));
      return;
   }
   
   if(result.retcode == TRADE_RETCODE_DONE || result.retcode == TRADE_RETCODE_PLACED)
   {
      if(EnableLogging)
         Print("BUY executed: Ticket #", result.order, " @ ", result.price);
      
      // Add to tracking
      AddPosition(result.order, "BUY", signal);
      g_lastError = "";
   }
   else
   {
      g_lastError = "BUY rejected: " + IntegerToString(result.retcode);
   }
}

//+------------------------------------------------------------------+
//| Execute SELL order                                                |
//+------------------------------------------------------------------+
void ExecuteSell(SignalData &signal)
{
   MqlTradeRequest request = {};
   MqlTradeResult result = {};
   
   request.action = TRADE_ACTION_DEAL;
   request.symbol = _Symbol;
   request.volume = signal.lotSize;
   request.type = ORDER_TYPE_SELL;
   request.price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   request.sl = signal.sl;
   request.tp = signal.tp;
   request.deviation = MaxSlippage;
   request.magic = MagicNumber;
   request.comment = signal.comment;
   request.type_filling = ORDER_FILLING_IOC;
   request.type_time = ORDER_TIME_GTC;
   
   if(!OrderSend(request, result))
   {
      g_lastError = "SELL failed: " + IntegerToString(result.retcode);
      if(EnableLogging)
         Print("SELL order failed: ", result.retcode, " - ", GetRetcodeDescription(result.retcode));
      return;
   }
   
   if(result.retcode == TRADE_RETCODE_DONE || result.retcode == TRADE_RETCODE_PLACED)
   {
      if(EnableLogging)
         Print("SELL executed: Ticket #", result.order, " @ ", result.price);
      
      // Add to tracking
      AddPosition(result.order, "SELL", signal);
      g_lastError = "";
   }
   else
   {
      g_lastError = "SELL rejected: " + IntegerToString(result.retcode);
   }
}

//+------------------------------------------------------------------+
//| Add position to tracking                                          |
//+------------------------------------------------------------------+
void AddPosition(ulong ticket, string direction, SignalData &signal)
{
   ArrayResize(g_positions, g_positionCount + 1);
   
   g_positions[g_positionCount].ticket = ticket;
   g_positions[g_positionCount].direction = direction;
   g_positions[g_positionCount].volume = signal.lotSize;
   g_positions[g_positionCount].openPrice = (direction == "BUY") ? 
      SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   g_positions[g_positionCount].currentSL = signal.sl;
   g_positions[g_positionCount].currentTP = signal.tp;
   g_positions[g_positionCount].openTime = TimeCurrent();
   g_positions[g_positionCount].breakevenHit = false;
   g_positions[g_positionCount].trailingActive = false;
   g_positions[g_positionCount].maxProfitPips = 0;
   g_positions[g_positionCount].breakevenTrigger = signal.breakevenTriggerPips;
   g_positions[g_positionCount].trailingTrigger = signal.trailingTriggerPips;
   g_positions[g_positionCount].trailingDistance = signal.trailingDistancePips;
   g_positions[g_positionCount].maxDrawdownPips = signal.maxDrawdownPips;
   
   g_positionCount++;
   g_statusDirty = true;
}

//+------------------------------------------------------------------+
//| Manage open positions (breakeven, trailing, drawdown)             |
//+------------------------------------------------------------------+
void ManagePositions()
{
   double pipSize = 0.1; // XAU/USD: 1 pip = 0.1
   
   for(int i = g_positionCount - 1; i >= 0; i--)
   {
      ulong ticket = g_positions[i].ticket;
      
      // Check if position still exists
      if(!PositionSelectByTicket(ticket))
      {
         // Position closed (by SL/TP or manually)
         RecordClosedPosition(i);
         RemovePosition(i);
         continue;
      }
      
      // Get current price
      double currentPrice = PositionGetDouble(POSITION_PRICE_CURRENT);
      double openPrice = g_positions[i].openPrice;
      string direction = g_positions[i].direction;
      
      // Calculate profit in pips
      double profitPips;
      if(direction == "BUY")
         profitPips = (currentPrice - openPrice) / pipSize;
      else
         profitPips = (openPrice - currentPrice) / pipSize;
      
      // Update max profit
      if(profitPips > g_positions[i].maxProfitPips)
      {
         g_positions[i].maxProfitPips = profitPips;
         g_statusDirty = true;
      }
      
      // Check max drawdown
      if(profitPips < -g_positions[i].maxDrawdownPips)
      {
         if(EnableLogging)
            Print("Max drawdown hit for #", ticket, " | Loss: ", profitPips, " pips");
         ClosePosition(ticket, "MaxDrawdown");
         continue;
      }
      
      // Check breakeven
      if(!g_positions[i].breakevenHit && profitPips >= g_positions[i].breakevenTrigger)
      {
         double spreadPips = 2.0;
         double newSL;
         
         if(direction == "BUY")
            newSL = openPrice + (spreadPips * pipSize);
         else
            newSL = openPrice - (spreadPips * pipSize);
         
         if(ModifySL(ticket, newSL))
         {
            g_positions[i].breakevenHit = true;
            g_positions[i].currentSL = newSL;
            g_statusDirty = true;
            
            if(EnableLogging)
               Print("Breakeven hit for #", ticket, " | New SL: ", newSL);
         }
      }
      
      // Check trailing stop (only after breakeven)
      if(g_positions[i].breakevenHit && profitPips >= g_positions[i].trailingTrigger)
      {
         g_positions[i].trailingActive = true;
         
         double trailDist = g_positions[i].trailingDistance * pipSize;
         double newSL;
         
         if(direction == "BUY")
            newSL = currentPrice - trailDist;
         else
            newSL = currentPrice + trailDist;
         
         // Only move SL if it improves
         bool shouldMove = false;
         if(direction == "BUY" && newSL > g_positions[i].currentSL)
            shouldMove = true;
         if(direction == "SELL" && newSL < g_positions[i].currentSL)
            shouldMove = true;
         
         if(shouldMove)
         {
            if(ModifySL(ticket, newSL))
            {
               if(EnableLogging)
                  Print("Trailing SL for #", ticket, " | ", 
                        g_positions[i].currentSL, " -> ", newSL);
               g_positions[i].currentSL = newSL;
               g_statusDirty = true;
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Modify SL of a position                                           |
//+------------------------------------------------------------------+
bool ModifySL(ulong ticket, double newSL)
{
   MqlTradeRequest request = {};
   MqlTradeResult result = {};
   
   if(!PositionSelectByTicket(ticket))
      return false;
   
   request.action = TRADE_ACTION_SLTP;
   request.symbol = _Symbol;
   request.position = ticket;
   request.sl = NormalizeDouble(newSL, _Digits);
   request.tp = PositionGetDouble(POSITION_TP);
   
   if(!OrderSend(request, result))
      return false;
   
   return (result.retcode == TRADE_RETCODE_DONE);
}

//+------------------------------------------------------------------+
//| Close a position                                                  |
//+------------------------------------------------------------------+
bool ClosePosition(ulong ticket, string reason)
{
   if(!PositionSelectByTicket(ticket))
      return false;
   
   MqlTradeRequest request = {};
   MqlTradeResult result = {};
   
   double volume = PositionGetDouble(POSITION_VOLUME);
   ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   
   request.action = TRADE_ACTION_DEAL;
   request.symbol = _Symbol;
   request.volume = volume;
   request.type = (posType == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   request.price = (posType == POSITION_TYPE_BUY) ? 
      SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   request.position = ticket;
   request.deviation = MaxSlippage;
   request.magic = MagicNumber;
   request.comment = "Close-" + reason;
   request.type_filling = ORDER_FILLING_IOC;
   
   if(!OrderSend(request, result))
   {
      if(EnableLogging)
         Print("Close failed for #", ticket, ": ", result.retcode);
      return false;
   }
   
   if(EnableLogging)
      Print("Position #", ticket, " closed: ", reason);
   
   return (result.retcode == TRADE_RETCODE_DONE);
}

//+------------------------------------------------------------------+
//| Close all positions with our magic number                         |
//+------------------------------------------------------------------+
void CloseAllPositions(string reason)
{
   for(int i = g_positionCount - 1; i >= 0; i--)
   {
      ClosePosition(g_positions[i].ticket, reason);
   }
}

//+------------------------------------------------------------------+
//| Record closed position                                            |
//+------------------------------------------------------------------+
void RecordClosedPosition(int index)
{
   // Get deal history for this position
   ulong ticket = g_positions[index].ticket;
   
   ArrayResize(g_closedToday, g_closedCount + 1);
   
   g_closedToday[g_closedCount].ticket = ticket;
   g_closedToday[g_closedCount].direction = g_positions[index].direction;
   g_closedToday[g_closedCount].volume = g_positions[index].volume;
   g_closedToday[g_closedCount].openPrice = g_positions[index].openPrice;
   g_closedToday[g_closedCount].closeTime = TimeCurrent();
   
   // Try to get close details from history
   HistorySelectByPosition(ticket);
   int deals = HistoryDealsTotal();
   
   for(int i = deals - 1; i >= 0; i--)
   {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0) continue;
      
      ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_INOUT || entry == DEAL_ENTRY_OUT_BY)
      {
         g_closedToday[g_closedCount].closePrice = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
         g_closedToday[g_closedCount].profit = HistoryDealGetDouble(dealTicket, DEAL_PROFIT);
         
         ENUM_DEAL_REASON reason = (ENUM_DEAL_REASON)HistoryDealGetInteger(dealTicket, DEAL_REASON);
         if(reason == DEAL_REASON_SL)
            g_closedToday[g_closedCount].closeReason = "SL";
         else if(reason == DEAL_REASON_TP)
            g_closedToday[g_closedCount].closeReason = "TP";
         else
            g_closedToday[g_closedCount].closeReason = "Manual";
         
         break;
      }
   }
   
   g_closedCount++;
   g_statusDirty = true;
   
   if(EnableLogging)
      Print("Position #", ticket, " closed and recorded");
}

//+------------------------------------------------------------------+
//| Remove position from tracking                                     |
//+------------------------------------------------------------------+
void RemovePosition(int index)
{
   for(int i = index; i < g_positionCount - 1; i++)
   {
      g_positions[i] = g_positions[i + 1];
   }
   g_positionCount--;
   ArrayResize(g_positions, g_positionCount);
}

//+------------------------------------------------------------------+
//| Update position data from MT5                                     |
//+------------------------------------------------------------------+
void UpdatePositionData()
{
   for(int i = 0; i < g_positionCount; i++)
   {
      if(PositionSelectByTicket(g_positions[i].ticket))
      {
         g_positions[i].currentSL = PositionGetDouble(POSITION_SL);
         g_positions[i].currentTP = PositionGetDouble(POSITION_TP);
      }
   }
}

//+------------------------------------------------------------------+
//| Write status JSON file                                            |
//+------------------------------------------------------------------+
bool WriteStatus()
{
   // Write directly to status file (terminal-specific MQL5\Files folder)
   int handle = FileOpen(StatusFile, FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      int err = GetLastError();
      g_lastWriteError = "FileOpen failed: " + IntegerToString(err);
      if(EnableLogging && g_consecutiveWriteFailures < 5)
         Print("Cannot write status file: ", err);
      return false;
   }
   
   // Build JSON
   string json = "{\n";
   json += "  \"version\": 1,\n";
   json += "  \"timestamp\": \"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",\n";
   json += "  \"last_signal_id\": \"" + g_lastSignalId + "\",\n";
   json += "  \"last_signal_result\": \"" + (g_lastError == "" ? "OK" : g_lastError) + "\",\n";
   
   // Account info
   json += "  \"account\": {\n";
   json += "    \"balance\": " + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + ",\n";
   json += "    \"equity\": " + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + ",\n";
   json += "    \"margin\": " + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + ",\n";
   json += "    \"free_margin\": " + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + "\n";
   json += "  },\n";
   
   // Positions
   json += "  \"positions\": [\n";
   for(int i = 0; i < g_positionCount; i++)
   {
      if(i > 0) json += ",\n";
      json += BuildPositionJson(i);
   }
   json += "\n  ],\n";
   
   // Closed today
   json += "  \"closed_today\": [\n";
   for(int i = 0; i < g_closedCount; i++)
   {
      if(i > 0) json += ",\n";
      json += BuildClosedJson(i);
   }
   json += "\n  ],\n";
   
   // Spread
   double spread = (SymbolInfoDouble(_Symbol, SYMBOL_ASK) - 
                    SymbolInfoDouble(_Symbol, SYMBOL_BID)) / 0.1;
   json += "  \"spread_pips\": " + DoubleToString(spread, 1) + ",\n";
   
   // Diagnostic fields for failure analysis
   json += "  \"heartbeat_count\": " + IntegerToString(g_heartbeatCount) + ",\n";
   json += "  \"last_heartbeat_time\": \"" + TimeToString(g_lastHeartbeatTime, TIME_DATE|TIME_SECONDS) + "\",\n";
   json += "  \"consecutive_write_failures\": " + IntegerToString(g_consecutiveWriteFailures) + ",\n";
   if(g_lastWriteError == "")
      json += "  \"last_write_error\": null,\n";
   else
      json += "  \"last_write_error\": \"" + g_lastWriteError + "\",\n";
   
   // Last error
   if(g_lastError == "")
      json += "  \"last_error\": null\n";
   else
      json += "  \"last_error\": \"" + g_lastError + "\"\n";
   
   json += "}\n";
   
   uint bytesWritten = FileWriteString(handle, json);
   FileClose(handle);
   
   if(bytesWritten == 0)
   {
      g_lastWriteError = "FileWriteString returned 0 bytes";
      return false;
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Build JSON for a position                                         |
//+------------------------------------------------------------------+
string BuildPositionJson(int index)
{
   PositionData pos = g_positions[index];
   double pipSize = 0.1;
   
   double currentPrice = 0;
   double profit = 0;
   double profitPips = 0;
   
   if(PositionSelectByTicket(pos.ticket))
   {
      currentPrice = PositionGetDouble(POSITION_PRICE_CURRENT);
      profit = PositionGetDouble(POSITION_PROFIT);
      
      if(pos.direction == "BUY")
         profitPips = (currentPrice - pos.openPrice) / pipSize;
      else
         profitPips = (pos.openPrice - currentPrice) / pipSize;
   }
   
   string phase = "OPEN";
   if(pos.trailingActive) phase = "TRAILING";
   else if(pos.breakevenHit) phase = "BREAKEVEN";
   
   string json = "    {\n";
   json += "      \"ticket\": " + IntegerToString(pos.ticket) + ",\n";
   json += "      \"direction\": \"" + pos.direction + "\",\n";
   json += "      \"volume\": " + DoubleToString(pos.volume, 2) + ",\n";
   json += "      \"open_price\": " + DoubleToString(pos.openPrice, 2) + ",\n";
   json += "      \"current_price\": " + DoubleToString(currentPrice, 2) + ",\n";
   json += "      \"sl\": " + DoubleToString(pos.currentSL, 2) + ",\n";
   json += "      \"tp\": " + DoubleToString(pos.currentTP, 2) + ",\n";
   json += "      \"profit\": " + DoubleToString(profit, 2) + ",\n";
   json += "      \"profit_pips\": " + DoubleToString(profitPips, 1) + ",\n";
   json += "      \"open_time\": \"" + TimeToString(pos.openTime, TIME_DATE|TIME_SECONDS) + "\",\n";
   json += "      \"phase\": \"" + phase + "\",\n";
   json += "      \"breakeven_hit\": " + (pos.breakevenHit ? "true" : "false") + ",\n";
   json += "      \"trailing_active\": " + (pos.trailingActive ? "true" : "false") + ",\n";
   json += "      \"max_profit_pips\": " + DoubleToString(pos.maxProfitPips, 1) + "\n";
   json += "    }";
   
   return json;
}

//+------------------------------------------------------------------+
//| Build JSON for a closed trade                                     |
//+------------------------------------------------------------------+
string BuildClosedJson(int index)
{
   ClosedTrade trade = g_closedToday[index];
   
   string json = "    {\n";
   json += "      \"ticket\": " + IntegerToString(trade.ticket) + ",\n";
   json += "      \"direction\": \"" + trade.direction + "\",\n";
   json += "      \"volume\": " + DoubleToString(trade.volume, 2) + ",\n";
   json += "      \"open_price\": " + DoubleToString(trade.openPrice, 2) + ",\n";
   json += "      \"close_price\": " + DoubleToString(trade.closePrice, 2) + ",\n";
   json += "      \"profit\": " + DoubleToString(trade.profit, 2) + ",\n";
   json += "      \"close_reason\": \"" + trade.closeReason + "\",\n";
   json += "      \"close_time\": \"" + TimeToString(trade.closeTime, TIME_DATE|TIME_SECONDS) + "\"\n";
   json += "    }";
   
   return json;
}

//+------------------------------------------------------------------+
//| Find an open chart by symbol and period                           |
//+------------------------------------------------------------------+
long FindChart(string symbol, ENUM_TIMEFRAMES period)
{
   long chart_id = ChartFirst();
   while(chart_id >= 0)
   {
      if(chart_id != ChartID())  // skip own chart
      {
         if(ChartSymbol(chart_id) == symbol && ChartPeriod(chart_id) == period)
            return chart_id;
      }
      chart_id = ChartNext(chart_id);
   }
   return -1;
}

//+------------------------------------------------------------------+
//| Check for screenshot request from Python                          |
//+------------------------------------------------------------------+
void CheckScreenshotRequest()
{
   if(!FileIsExist(ScreenshotRequestFile))
      return;

   // Read the request file
   int handle = FileOpen(ScreenshotRequestFile, FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
      return;

   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);

   if(StringLen(content) < 5)
      return;

   int reqWidth  = (int)GetJsonInt(content, "width");
   int reqHeight = (int)GetJsonInt(content, "height");
   if(reqWidth  <= 0) reqWidth  = 1280;
   if(reqHeight <= 0) reqHeight = 720;

   // Capture H1 (own chart, chart_id = 0)
   bool h1_ok = ChartScreenShot(0, ScreenshotH1File, reqWidth, reqHeight, ALIGN_RIGHT);
   if(EnableLogging)
      Print("Screenshot H1: ", h1_ok ? "OK" : "FAILED");

   // Find and capture M15 chart
   bool m15_ok = false;
   long m15_chart_id = FindChart(_Symbol, PERIOD_M15);
   if(m15_chart_id > 0)
   {
      m15_ok = ChartScreenShot(m15_chart_id, ScreenshotM15File, reqWidth, reqHeight, ALIGN_RIGHT);
      if(EnableLogging)
         Print("Screenshot M15 (chart ", m15_chart_id, "): ", m15_ok ? "OK" : "FAILED");
   }
   else
   {
      // Try to find any XAUUSD chart that isn't our timeframe
      if(EnableLogging)
         Print("Screenshot M15: XAUUSD M15 chart not found (not open in MT5)");
   }

   // Write screenshot_ready.json
   int wHandle = FileOpen(ScreenshotReadyFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(wHandle != INVALID_HANDLE)
   {
      string json = "{";
      json += "\"version\":1,";
      json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "\",";
      json += "\"h1_file\":\"" + (h1_ok ? ScreenshotH1File : "") + "\",";
      json += "\"h1_ok\":" + (h1_ok ? "true" : "false") + ",";
      json += "\"m15_file\":\"" + (m15_ok ? ScreenshotM15File : "") + "\",";
      json += "\"m15_ok\":" + (m15_ok ? "true" : "false");
      json += "}";
      FileWriteString(wHandle, json);
      FileClose(wHandle);
   }

   // Delete the request file (signal consumed)
   FileDelete(ScreenshotRequestFile);

   if(EnableLogging)
      Print("Screenshot request processed: H1=", h1_ok, " M15=", m15_ok);
}

//+------------------------------------------------------------------+
//| JSON parsing helpers (simple implementation)                      |
//+------------------------------------------------------------------+
string GetJsonString(string json, string key)
{
   string searchKey = "\"" + key + "\"";
   int pos = StringFind(json, searchKey);
   if(pos < 0) return "";
   
   pos = StringFind(json, ":", pos);
   if(pos < 0) return "";
   
   // Find opening quote
   int start = StringFind(json, "\"", pos + 1);
   if(start < 0) return "";
   
   // Find closing quote
   int end = StringFind(json, "\"", start + 1);
   if(end < 0) return "";
   
   return StringSubstr(json, start + 1, end - start - 1);
}

double GetJsonDouble(string json, string key)
{
   string searchKey = "\"" + key + "\"";
   int pos = StringFind(json, searchKey);
   if(pos < 0) return 0;
   
   pos = StringFind(json, ":", pos);
   if(pos < 0) return 0;
   
   // Skip whitespace
   pos++;
   while(pos < StringLen(json) && (StringGetCharacter(json, pos) == ' ' || 
         StringGetCharacter(json, pos) == '\t' || StringGetCharacter(json, pos) == '\n'))
      pos++;
   
   // Find end of number
   int start = pos;
   while(pos < StringLen(json))
   {
      ushort c = StringGetCharacter(json, pos);
      if((c >= '0' && c <= '9') || c == '.' || c == '-' || c == '+')
         pos++;
      else
         break;
   }
   
   string numStr = StringSubstr(json, start, pos - start);
   return StringToDouble(numStr);
}

long GetJsonInt(string json, string key)
{
   return (long)GetJsonDouble(json, key);
}

//+------------------------------------------------------------------+
//| Get retcode description                                           |
//+------------------------------------------------------------------+
string GetRetcodeDescription(uint retcode)
{
   switch(retcode)
   {
      case TRADE_RETCODE_REQUOTE: return "Requote";
      case TRADE_RETCODE_REJECT: return "Rejected";
      case TRADE_RETCODE_CANCEL: return "Cancelled";
      case TRADE_RETCODE_PLACED: return "Placed";
      case TRADE_RETCODE_DONE: return "Done";
      case TRADE_RETCODE_DONE_PARTIAL: return "Partial";
      case TRADE_RETCODE_ERROR: return "Error";
      case TRADE_RETCODE_TIMEOUT: return "Timeout";
      case TRADE_RETCODE_INVALID: return "Invalid";
      case TRADE_RETCODE_INVALID_VOLUME: return "Invalid volume";
      case TRADE_RETCODE_INVALID_PRICE: return "Invalid price";
      case TRADE_RETCODE_INVALID_STOPS: return "Invalid stops";
      case TRADE_RETCODE_TRADE_DISABLED: return "Trade disabled";
      case TRADE_RETCODE_MARKET_CLOSED: return "Market closed";
      case TRADE_RETCODE_NO_MONEY: return "No money";
      case TRADE_RETCODE_PRICE_CHANGED: return "Price changed";
      case TRADE_RETCODE_PRICE_OFF: return "Price off";
      default: return "Unknown (" + IntegerToString(retcode) + ")";
   }
}

//+------------------------------------------------------------------+
//| FLO-231: Price Alert — tick-level monitoring                      |
//+------------------------------------------------------------------+
void CheckPriceAlerts()
{
   // Re-read alert file every N seconds (not every tick)
   if(TimeCurrent() - g_lastAlertFileCheck >= g_alertCheckInterval)
   {
      g_lastAlertFileCheck = TimeCurrent();
      ReadPriceAlerts();
   }

   if(g_alertCount == 0) return;

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   for(int i = 0; i < g_alertCount; i++)
   {
      if(g_alerts[i].triggered) continue;

      bool touched = false;
      if(g_alerts[i].alertType == "price_above" && bid >= g_alerts[i].level)
         touched = true;
      else if(g_alerts[i].alertType == "price_below" && bid <= g_alerts[i].level)
         touched = true;
      else if(g_alerts[i].alertType == "price_touch" && MathAbs(bid - g_alerts[i].level) <= 1.0)
         touched = true;

      if(touched)
      {
         g_alerts[i].triggered  = true;
         g_alerts[i].touchTime  = TimeCurrent();
         g_alerts[i].touchPrice = bid;

         WritePriceAlertTrigger(g_alerts[i], bid);

         if(EnableLogging)
            Print("FLOKI_ALERT | ", g_alerts[i].id, " | level=",
                  DoubleToString(g_alerts[i].level, 2), " touched at bid=",
                  DoubleToString(bid, 2));
      }
   }
}

//+------------------------------------------------------------------+
//| Read price_alerts.json and update alert array + chart lines       |
//+------------------------------------------------------------------+
void ReadPriceAlerts()
{
   if(!FileIsExist(PriceAlertFile))
   {
      if(g_alertCount > 0)
      {
         RemoveAlertLines();
         g_alertCount = 0;
         ArrayResize(g_alerts, 0);
      }
      return;
   }

   int handle = FileOpen(PriceAlertFile, FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE) return;

   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);

   if(StringLen(content) < 5) return;

   // Check version to avoid redundant redraws
   double ver = GetJsonDouble(content, "version");
   string tsStr = GetJsonString(content, "timestamp");

   // Parse alerts array — count occurrences of "id" to determine array size
   int count = 0;
   int searchPos = 0;
   while(true)
   {
      int found = StringFind(content, "\"id\"", searchPos);
      if(found < 0) break;
      count++;
      searchPos = found + 4;
   }

   if(count == 0)
   {
      if(g_alertCount > 0)
      {
         RemoveAlertLines();
         g_alertCount = 0;
         ArrayResize(g_alerts, 0);
      }
      return;
   }

   // Parse each alert object
   ArrayResize(g_alerts, count);
   g_alertCount = count;

   searchPos = 0;
   for(int i = 0; i < count; i++)
   {
      // Find the i-th alert block (starting from each "id" occurrence)
      int idPos = StringFind(content, "\"id\"", searchPos);
      if(idPos < 0) break;

      // Extract a substring around this alert (~200 chars should cover one object)
      int blockStart = idPos - 10;
      if(blockStart < 0) blockStart = 0;
      int blockEnd = blockStart + 200;
      if(blockEnd > StringLen(content)) blockEnd = StringLen(content);
      string block = StringSubstr(content, blockStart, blockEnd - blockStart);

      g_alerts[i].id        = GetJsonString(block, "id");
      g_alerts[i].alertType = GetJsonString(block, "type");
      g_alerts[i].level     = GetJsonDouble(block, "level");
      g_alerts[i].triggered = false;
      g_alerts[i].touchTime = 0;
      g_alerts[i].touchPrice = 0;

      searchPos = idPos + 4;
   }

   DrawAlertLines();
}

//+------------------------------------------------------------------+
//| Draw hot-pink dashed lines at alert levels                        |
//+------------------------------------------------------------------+
void DrawAlertLines()
{
   RemoveAlertLines();

   for(int i = 0; i < g_alertCount; i++)
   {
      string name  = ALERT_LINE_PREFIX + g_alerts[i].id;
      double level = g_alerts[i].level;
      string arrow = (g_alerts[i].alertType == "price_above") ? "\x2191 " : "\x2193 ";
      string label = arrow + DoubleToString(level, 1);

      // Horizontal line
      ObjectCreate(0, name, OBJ_HLINE, 0, 0, level);
      ObjectSetInteger(0, name, OBJPROP_COLOR, clrHotPink);
      ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DASH);
      ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, name, OBJPROP_BACK, false);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetString(0, name, OBJPROP_TEXT, label);
      ObjectSetString(0, name, OBJPROP_TOOLTIP, label);

      // Right-aligned text label
      string lblName = ALERT_LINE_PREFIX + "LBL_" + g_alerts[i].id;
      ObjectCreate(0, lblName, OBJ_TEXT, 0, TimeCurrent(), level);
      ObjectSetString(0, lblName, OBJPROP_TEXT, label);
      ObjectSetInteger(0, lblName, OBJPROP_COLOR, clrHotPink);
      ObjectSetInteger(0, lblName, OBJPROP_FONTSIZE, 8);
      ObjectSetString(0, lblName, OBJPROP_FONT, "Arial Bold");
      ObjectSetInteger(0, lblName, OBJPROP_ANCHOR, ANCHOR_RIGHT);
   }
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| Remove all FLOKI_ALERT_ objects from chart                        |
//+------------------------------------------------------------------+
void RemoveAlertLines()
{
   int total = ObjectsTotal(0);
   for(int i = total - 1; i >= 0; i--)
   {
      string name = ObjectName(0, i);
      if(StringFind(name, ALERT_LINE_PREFIX) == 0)
         ObjectDelete(0, name);
   }
}

//+------------------------------------------------------------------+
//| Write trigger JSON when a price level is touched                  |
//+------------------------------------------------------------------+
void WritePriceAlertTrigger(PriceAlert &alert, double currentBid)
{
   int handle = FileOpen(PriceAlertTriggerFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE) return;

   string direction = (alert.alertType == "price_above") ? "touched_above" : "touched_below";

   string json = "{";
   json += "\"version\":1,";
   json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "\",";
   json += "\"alert_id\":\"" + alert.id + "\",";
   json += "\"level\":" + DoubleToString(alert.level, 2) + ",";
   json += "\"touch_price\":" + DoubleToString(alert.touchPrice, 2) + ",";
   json += "\"touch_time\":\"" + TimeToString(alert.touchTime, TIME_DATE | TIME_SECONDS) + "\",";
   json += "\"current_price\":" + DoubleToString(currentBid, 2) + ",";
   json += "\"bounce_pips\":0.0,";
   json += "\"direction\":\"" + direction + "\"";
   json += "}";

   FileWriteString(handle, json);
   FileClose(handle);
}
//+------------------------------------------------------------------+
