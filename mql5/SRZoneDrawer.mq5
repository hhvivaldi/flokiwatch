//+------------------------------------------------------------------+
//|                                              SRZoneDrawer.mq5    |
//|                        Draws S/R zones on chart from Python bot   |
//|                        Reads sr_zones.json from MQL5\Files\       |
//+------------------------------------------------------------------+
#property copyright "XAU/USD Trading Bot"
#property version   "1.00"
#property description "Draws S/R horizontal lines + labels from Python bot sr_zones.json"

//--- Inputs
input int    RefreshSeconds   = 10;       // Check JSON every N seconds
input string InputFileName    = "sr_zones.json";
input string ObjectPrefix     = "SR_";    // Prefix for all objects (only these get deleted)
input int    LabelFontSize    = 8;        // Font size for zone labels
input string LabelFont        = "Arial";  // Font for zone labels

//--- Colors
input color  ColorSupport     = clrLime;      // Support zone color
input color  ColorResistance  = clrTomato;    // Resistance zone color
input color  ColorFlip        = clrGold;      // Flip zone color

//--- State
string LastUpdatedAt = "";   // Track last JSON timestamp to avoid redundant redraws

//+------------------------------------------------------------------+
//| Expert initialization                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(RefreshSeconds);
   Print("SRZoneDrawer: Started | Refresh: ", RefreshSeconds, "s | File: ", InputFileName);
   // Draw immediately on attach
   ReadAndDraw();
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Expert deinitialization                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   DeleteAllSRObjects();
   Print("SRZoneDrawer: Stopped, all SR_ objects removed");
  }

//+------------------------------------------------------------------+
//| Timer event — periodic refresh                                    |
//+------------------------------------------------------------------+
void OnTimer()
  {
   ReadAndDraw();
  }

//+------------------------------------------------------------------+
//| Delete all objects with our prefix                                |
//+------------------------------------------------------------------+
void DeleteAllSRObjects()
  {
   ObjectsDeleteAll(0, ObjectPrefix);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| Main: read JSON and draw zones                                    |
//+------------------------------------------------------------------+
void ReadAndDraw()
  {
   // Read JSON file
   string json_content = ReadJSONFile();
   if(json_content == "")
      return;

   // Parse updated_at to check if data changed
   string updated_at = ExtractStringField(json_content, "updated_at");
   if(updated_at == LastUpdatedAt && updated_at != "")
      return;  // No change since last draw

   LastUpdatedAt = updated_at;

   // Parse zones count
   int zones_count = (int)ExtractNumberField(json_content, "zones_count");
   if(zones_count <= 0)
     {
      DeleteAllSRObjects();
      return;
     }

   // Delete old objects before drawing new ones
   DeleteAllSRObjects();

   // Parse and draw each zone
   string zones_array = ExtractArrayField(json_content, "zones");
   if(zones_array == "")
      return;

   // Split zones array into individual zone objects
   string zone_objects[];
   int count = SplitJSONArray(zones_array, zone_objects);

   // Get the time coordinate for labels (right edge of visible chart)
   datetime label_time = GetRightEdgeTime();

   for(int i = 0; i < count && i < 8; i++)
     {
      string z = zone_objects[i];

      double price      = ExtractNumberField(z, "price");
      string zone_type  = ExtractStringField(z, "zone_type");
      int    touches    = (int)ExtractNumberField(z, "touches");
      string timeframe  = ExtractStringField(z, "timeframe");
      string strength   = ExtractStringField(z, "strength");
      string position   = ExtractStringField(z, "position");
      string flip_phase = ExtractStringField(z, "flip_phase");

      // Parse confluence array
      bool has_mtf = false;
      string confluence_str = ExtractArrayField(z, "confluence");
      if(confluence_str != "")
        {
         // Count commas to determine number of TFs
         int comma_count = 0;
         for(int c = 0; c < StringLen(confluence_str); c++)
           {
            if(StringGetCharacter(confluence_str, c) == ',')
               comma_count++;
           }
         if(comma_count >= 1)
            has_mtf = true;
        }

      if(price <= 0)
         continue;

      // Determine color — flip zones (with flip_phase) keep gold color
      bool is_flip = (flip_phase == "R_TO_S" || flip_phase == "S_TO_R");
      color line_color = ColorSupport;
      if(is_flip)
         line_color = ColorFlip;
      else if(zone_type == "RESISTANCE")
         line_color = ColorResistance;

      // Determine line width based on rules:
      // D1 or MTF confluence → 3 (thick)
      // H4 or ≥4 touches → 2 (medium)
      // H1 only, <4 touches → 1 (thin)
      int line_width = 1;
      if(timeframe == "D1" || has_mtf)
         line_width = 3;
      else if(timeframe == "H4" || touches >= 4)
         line_width = 2;

      // Draw horizontal line
      string line_name = ObjectPrefix + "LINE_" + IntegerToString(i);
      ObjectCreate(0, line_name, OBJ_HLINE, 0, 0, price);
      ObjectSetInteger(0, line_name, OBJPROP_COLOR, line_color);
      ObjectSetInteger(0, line_name, OBJPROP_WIDTH, line_width);
      ObjectSetInteger(0, line_name, OBJPROP_STYLE, STYLE_SOLID);
      ObjectSetInteger(0, line_name, OBJPROP_BACK, true);
      ObjectSetInteger(0, line_name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, line_name, OBJPROP_HIDDEN, true);

      // Build label text: "[TF] [MTF?] [TYPE_SHORT] [(phase)] [N]T"
      string type_short = "SUP";
      if(zone_type == "RESISTANCE")
         type_short = "RES";

      string phase_tag = "";
      if(flip_phase == "R_TO_S")
         phase_tag = " (R>S)";
      else if(flip_phase == "S_TO_R")
         phase_tag = " (S>R)";

      string mtf_tag = has_mtf ? " MTF" : "";
      string label_text = timeframe + mtf_tag + " " + type_short + phase_tag + " " + IntegerToString(touches) + "T";

      // Draw text label at right edge of chart
      string label_name = ObjectPrefix + "LABEL_" + IntegerToString(i);
      ObjectCreate(0, label_name, OBJ_TEXT, 0, label_time, price);
      ObjectSetString(0, label_name, OBJPROP_TEXT, label_text);
      ObjectSetString(0, label_name, OBJPROP_FONT, LabelFont);
      ObjectSetInteger(0, label_name, OBJPROP_FONTSIZE, LabelFontSize);
      ObjectSetInteger(0, label_name, OBJPROP_COLOR, line_color);
      ObjectSetInteger(0, label_name, OBJPROP_ANCHOR, ANCHOR_RIGHT);
      ObjectSetInteger(0, label_name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, label_name, OBJPROP_HIDDEN, true);
     }

   ChartRedraw(0);
   Print("SRZoneDrawer: Drew ", MathMin(count, 8), " zones | Updated: ", updated_at);
  }

//+------------------------------------------------------------------+
//| Get the time at the right edge of the visible chart               |
//+------------------------------------------------------------------+
datetime GetRightEdgeTime()
  {
   // Get the last visible bar time and add a small offset
   int visible_bars = (int)ChartGetInteger(0, CHART_VISIBLE_BARS);
   int first_visible = (int)ChartGetInteger(0, CHART_FIRST_VISIBLE_BAR);

   // The rightmost visible bar index (from current bar)
   int right_bar = first_visible - visible_bars + 1;
   if(right_bar < 0)
      right_bar = 0;

   // Use the time of the rightmost visible bar
   datetime bar_times[];
   if(CopyTime(_Symbol, PERIOD_CURRENT, right_bar, 1, bar_times) > 0)
      return bar_times[0];

   // Fallback: use current time
   return TimeCurrent();
  }

//+------------------------------------------------------------------+
//| Read JSON file contents                                           |
//+------------------------------------------------------------------+
string ReadJSONFile()
  {
   if(!FileIsExist(InputFileName))
      return "";

   int handle = FileOpen(InputFileName, FILE_READ | FILE_TXT | FILE_ANSI);
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
//| Extract a string field value from JSON (simple parser)            |
//+------------------------------------------------------------------+
string ExtractStringField(const string &json, const string field)
  {
   string search = "\"" + field + "\"";
   int pos = StringFind(json, search);
   if(pos < 0)
      return "";

   // Find the colon after the field name
   int colon_pos = StringFind(json, ":", pos + StringLen(search));
   if(colon_pos < 0)
      return "";

   // Find opening quote of value
   int quote_start = StringFind(json, "\"", colon_pos + 1);
   if(quote_start < 0)
      return "";

   // Find closing quote
   int quote_end = StringFind(json, "\"", quote_start + 1);
   if(quote_end < 0)
      return "";

   return StringSubstr(json, quote_start + 1, quote_end - quote_start - 1);
  }

//+------------------------------------------------------------------+
//| Extract a number field value from JSON                            |
//+------------------------------------------------------------------+
double ExtractNumberField(const string &json, const string field)
  {
   string search = "\"" + field + "\"";
   int pos = StringFind(json, search);
   if(pos < 0)
      return 0;

   // Find the colon
   int colon_pos = StringFind(json, ":", pos + StringLen(search));
   if(colon_pos < 0)
      return 0;

   // Extract number: skip whitespace, read until comma/bracket/newline
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
//| Extract an array field from JSON (returns content between [])     |
//+------------------------------------------------------------------+
string ExtractArrayField(const string &json, const string field)
  {
   string search = "\"" + field + "\"";
   int pos = StringFind(json, search);
   if(pos < 0)
      return "";

   // Find opening bracket
   int bracket_start = StringFind(json, "[", pos);
   if(bracket_start < 0)
      return "";

   // Find matching closing bracket (handle nesting)
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
//| Split a JSON array string into individual objects                  |
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
