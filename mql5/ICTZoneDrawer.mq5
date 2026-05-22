//+------------------------------------------------------------------+
//|                                            ICTZoneDrawer.mq5      |
//|        Draws ICT zones (FVG rectangles + liquidity sweeps) on the |
//|        chart from the Python bot's ict_zones.json (FLO-455 Ph2).  |
//|        Bridge pattern — same as SRZoneDrawer.mq5. ADDITIVE: uses  |
//|        a separate ICT_ object prefix, never touches SR_ objects.  |
//+------------------------------------------------------------------+
#property copyright "XAU/USD Trading Bot"
#property version   "1.00"
#property description "Draws ICT FVG rectangles + liquidity sweep lines from ict_zones.json"

//--- Inputs
input int    RefreshSeconds  = 10;          // Check JSON every N seconds (match SRZoneDrawer)
input string ObjectPrefix    = "ICT_";      // Prefix — ONLY these objects get deleted
input bool   ShowLabels      = true;        // Draw a small label per zone
input int    LabelFontSize   = 7;           // Label font size
input string LabelFont       = "Arial";     // Label font
input int    RightExtendBars = 6;           // Bars to extend zones to the right of now

//--- Colors (per FLO-455 spec)
input color  ColorFVGBull    = clrLimeGreen;  // Bullish FVG rectangle (green)
input color  ColorFVGBear    = clrCrimson;    // Bearish FVG rectangle (red)
input color  ColorSweepBSL   = clrTomato;     // BSL sweep (direction "high") — red
input color  ColorSweepSSL   = clrLimeGreen;  // SSL sweep (direction "low")  — green

//--- State
string ActiveFileName = "ict_zones.json";   // single H1 file (FLO-455 Phase 1)
string LastTimestamp  = "";                 // skip redraw when JSON unchanged
int    BrokerOffsetSec = 0;                 // server(broker) − GMT, to align UTC candle_time

//+------------------------------------------------------------------+
//| Init                                                              |
//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(RefreshSeconds);
   BrokerOffsetSec = (int)(TimeCurrent() - TimeGMT());   // candle_time is UTC; chart is broker time
   Print("ICTZoneDrawer: started | refresh ", RefreshSeconds, "s | file ", ActiveFileName,
         " | broker offset ", BrokerOffsetSec / 3600, "h");
   ReadAndDraw();   // draw immediately on attach
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Deinit — remove only our objects                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   ObjectsDeleteAll(0, ObjectPrefix);
   ChartRedraw(0);
   Print("ICTZoneDrawer: stopped, all ", ObjectPrefix, " objects removed");
  }

//+------------------------------------------------------------------+
//| Timer — periodic refresh                                          |
//+------------------------------------------------------------------+
void OnTimer()
  {
   ReadAndDraw();
  }

//+------------------------------------------------------------------+
//| Main: read JSON, redraw all ICT zones                             |
//+------------------------------------------------------------------+
void ReadAndDraw()
  {
   string json = ReadJSONFile();
   if(json == "")
      return;

   // Skip redraw if the JSON hasn't changed since last cycle.
   string ts = ExtractStringField(json, "timestamp");
   if(ts == LastTimestamp && ts != "")
      return;
   LastTimestamp = ts;

   // Delete-all-by-prefix then redraw = automatic cleanup of zones that
   // dropped out of the JSON (mitigated/expired).
   ObjectsDeleteAll(0, ObjectPrefix);

   string zones = ExtractArrayField(json, "zones");
   if(zones == "")
     {
      ChartRedraw(0);
      return;
     }

   string objs[];
   int n = SplitJSONArray(zones, objs);
   datetime right_edge = TimeCurrent() + (datetime)(PeriodSeconds(PERIOD_CURRENT) * RightExtendBars);

   for(int i = 0; i < n; i++)
     {
      string z = objs[i];
      string ztype = ExtractStringField(z, "type");
      string dir   = ExtractStringField(z, "direction");
      datetime ct  = ISOToTime(ExtractStringField(z, "candle_time")) + BrokerOffsetSec;
      if(ct <= 0)
         ct = right_edge - (datetime)(PeriodSeconds(PERIOD_CURRENT) * 20);  // fallback anchor

      if(ztype == "FVG")
        {
         double top = ExtractNumberField(z, "top");
         double bot = ExtractNumberField(z, "bottom");
         if(top <= 0 || bot <= 0)
            continue;
         color col = (dir == "bullish") ? ColorFVGBull : ColorFVGBear;
         string nm = ObjectPrefix + "FVG_" + IntegerToString(i);
         ObjectCreate(0, nm, OBJ_RECTANGLE, 0, ct, top, right_edge, bot);
         ObjectSetInteger(0, nm, OBJPROP_COLOR, col);
         ObjectSetInteger(0, nm, OBJPROP_FILL, true);   // filled box...
         ObjectSetInteger(0, nm, OBJPROP_BACK, true);   // ...behind candles -> reads semi-transparent
         ObjectSetInteger(0, nm, OBJPROP_WIDTH, 1);
         ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
         if(ShowLabels)
            DrawLabel(ObjectPrefix + "FVGL_" + IntegerToString(i), right_edge, (top + bot) / 2.0,
                      "H1 FVG " + (dir == "bullish" ? "up" : "dn"), col);
        }
      else if(ztype == "SWEEP")
        {
         double lvl = ExtractNumberField(z, "level");
         if(lvl <= 0)
            continue;
         bool   bsl = (dir == "high");                  // "high" = buy-side liquidity (BSL)
         color  col = bsl ? ColorSweepBSL : ColorSweepSSL;
         // Horizontal dotted segment from the sweep candle to the right edge.
         string ln = ObjectPrefix + "SWP_" + IntegerToString(i);
         ObjectCreate(0, ln, OBJ_TREND, 0, ct, lvl, right_edge, lvl);
         ObjectSetInteger(0, ln, OBJPROP_COLOR, col);
         ObjectSetInteger(0, ln, OBJPROP_STYLE, STYLE_DOT);
         ObjectSetInteger(0, ln, OBJPROP_WIDTH, 1);
         ObjectSetInteger(0, ln, OBJPROP_RAY_RIGHT, false);
         ObjectSetInteger(0, ln, OBJPROP_SELECTABLE, false);
         // Arrow at the right edge: BSL = up (red), SSL = down (green).
         string ar = ObjectPrefix + "ARR_" + IntegerToString(i);
         ObjectCreate(0, ar, bsl ? OBJ_ARROW_UP : OBJ_ARROW_DOWN, 0, right_edge, lvl);
         ObjectSetInteger(0, ar, OBJPROP_COLOR, col);
         ObjectSetInteger(0, ar, OBJPROP_WIDTH, 2);
         ObjectSetInteger(0, ar, OBJPROP_SELECTABLE, false);
         if(ShowLabels)
            DrawLabel(ObjectPrefix + "SWPL_" + IntegerToString(i), right_edge, lvl,
                      "H1 SWEEP " + (bsl ? "BSL" : "SSL"), col);
        }
     }

   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| Draw a right-aligned text label                                   |
//+------------------------------------------------------------------+
void DrawLabel(const string name, const datetime t, const double price,
               const string text, const color col)
  {
   ObjectCreate(0, name, OBJ_TEXT, 0, t, price);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, LabelFont);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, LabelFontSize);
   ObjectSetInteger(0, name, OBJPROP_COLOR, col);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
  }

//+------------------------------------------------------------------+
//| Convert ISO-8601 UTC "2026-05-19T04:00:00Z" -> datetime           |
//+------------------------------------------------------------------+
datetime ISOToTime(const string iso)
  {
   if(StringLen(iso) < 19)
      return 0;
   string s = StringSubstr(iso, 0, 19);   // "2026-05-19T04:00:00"
   StringReplace(s, "-", ".");
   StringReplace(s, "T", " ");            // -> "2026.05.19 04:00:00"
   return StringToTime(s);
  }

//+------------------------------------------------------------------+
//| Read the whole JSON file into a string                            |
//+------------------------------------------------------------------+
string ReadJSONFile()
  {
   if(!FileIsExist(ActiveFileName))
      return "";
   int handle = FileOpen(ActiveFileName, FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
      return "";
   string content = "";
   while(!FileIsEnding(handle))
     {
      content += FileReadString(handle);
      if(!FileIsEnding(handle))
         content += "\n";
     }
   FileClose(handle);
   return content;
  }

//+------------------------------------------------------------------+
//| Extract a string field "field":"value"                            |
//+------------------------------------------------------------------+
string ExtractStringField(const string &json, const string field)
  {
   string search = "\"" + field + "\"";
   int pos = StringFind(json, search);
   if(pos < 0)
      return "";
   int colon_pos = StringFind(json, ":", pos + StringLen(search));
   if(colon_pos < 0)
      return "";
   int quote_start = StringFind(json, "\"", colon_pos + 1);
   if(quote_start < 0)
      return "";
   int quote_end = StringFind(json, "\"", quote_start + 1);
   if(quote_end < 0)
      return "";
   return StringSubstr(json, quote_start + 1, quote_end - quote_start - 1);
  }

//+------------------------------------------------------------------+
//| Extract a numeric field "field": number                           |
//+------------------------------------------------------------------+
double ExtractNumberField(const string &json, const string field)
  {
   string search = "\"" + field + "\"";
   int pos = StringFind(json, search);
   if(pos < 0)
      return 0;
   int colon_pos = StringFind(json, ":", pos + StringLen(search));
   if(colon_pos < 0)
      return 0;
   string num_str = "";
   for(int i = colon_pos + 1; i < StringLen(json); i++)
     {
      ushort ch = StringGetCharacter(json, i);
      if(ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r')
        {
         if(num_str != "")
            break;
         continue;
        }
      if(ch == ',' || ch == '}' || ch == ']')
         break;
      num_str += ShortToString(ch);
     }
   return StringToDouble(num_str);
  }

//+------------------------------------------------------------------+
//| Extract an array field — content between [ ]                       |
//+------------------------------------------------------------------+
string ExtractArrayField(const string &json, const string field)
  {
   string search = "\"" + field + "\"";
   int pos = StringFind(json, search);
   if(pos < 0)
      return "";
   int bracket_start = StringFind(json, "[", pos);
   if(bracket_start < 0)
      return "";
   int depth = 0;
   int bracket_end = -1;
   for(int i = bracket_start; i < StringLen(json); i++)
     {
      ushort ch = StringGetCharacter(json, i);
      if(ch == '[')
         depth++;
      else if(ch == ']')
        {
         depth--;
         if(depth == 0)
           {
            bracket_end = i;
            break;
           }
        }
     }
   if(bracket_end < 0)
      return "";
   return StringSubstr(json, bracket_start + 1, bracket_end - bracket_start - 1);
  }

//+------------------------------------------------------------------+
//| Split a JSON array string into individual { } objects             |
//+------------------------------------------------------------------+
int SplitJSONArray(const string &array_content, string &objects[])
  {
   int count = 0;
   int depth = 0;
   int obj_start = -1;
   for(int i = 0; i < StringLen(array_content); i++)
     {
      ushort ch = StringGetCharacter(array_content, i);
      if(ch == '{')
        {
         if(depth == 0)
            obj_start = i;
         depth++;
        }
      else if(ch == '}')
        {
         depth--;
         if(depth == 0 && obj_start >= 0)
           {
            ArrayResize(objects, count + 1);
            objects[count] = StringSubstr(array_content, obj_start, i - obj_start + 1);
            count++;
            obj_start = -1;
           }
        }
     }
   return count;
  }
//+------------------------------------------------------------------+
