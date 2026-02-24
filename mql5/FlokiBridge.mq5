//+------------------------------------------------------------------+
//|                                                  FlokiBridge.mq5 |
//|                                        FlokiWatch Trading System |
//|                           Execution Bridge for Python Brain      |
//+------------------------------------------------------------------+
#property copyright "FlokiWatch"
#property link      ""
#property version   "1.00"
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

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit()
{
   // Set timer for status updates
   EventSetMillisecondTimer(StatusUpdateMs);
   
   // Initialize arrays
   ArrayResize(g_positions, 0);
   ArrayResize(g_closedToday, 0);
   g_positionCount = 0;
   g_closedCount = 0;
   
   // Scan existing positions
   ScanExistingPositions();
   
   if(EnableLogging)
      Print("FlokiBridge initialized. Magic: ", MagicNumber, 
            " | Positions: ", g_positionCount);
   
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
   if(EnableLogging)
      Print("FlokiBridge stopped. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| Timer function - writes status periodically                       |
//+------------------------------------------------------------------+
void OnTimer()
{
   // Update position data
   UpdatePositionData();
   
   // Write status if dirty or periodically
   if(g_statusDirty)
   {
      WriteStatus();
      g_statusDirty = false;
   }
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick()
{
   // Check for new signal (only if file changed)
   CheckSignalFile();
   
   // Manage open positions (breakeven, trailing, drawdown)
   ManagePositions();
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
   // Check if file exists
   if(!FileIsExist(SignalFile, FILE_COMMON))
      return;
   
   // Check file modification time
   datetime fileTime = (datetime)FileGetInteger(SignalFile, FILE_MODIFY_DATE, FILE_COMMON);
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
   int handle = FileOpen(SignalFile, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
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
void WriteStatus()
{
   // Write to temp file first, then rename (atomic)
   string tempFile = StatusFile + ".tmp";
   
   int handle = FileOpen(tempFile, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(handle == INVALID_HANDLE)
   {
      if(EnableLogging)
         Print("Cannot write status file");
      return;
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
   
   // Last error
   if(g_lastError == "")
      json += "  \"last_error\": null\n";
   else
      json += "  \"last_error\": \"" + g_lastError + "\"\n";
   
   json += "}\n";
   
   FileWriteString(handle, json);
   FileClose(handle);
   
   // Rename temp to final (atomic on most systems)
   FileDelete(StatusFile, FILE_COMMON);
   FileMove(tempFile, 0, StatusFile, FILE_COMMON);
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
