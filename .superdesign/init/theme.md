# Theme

## Compact token summary

- Surface: `#0c0d14` page background; `#151621` panel/card surface; translucent
  navy `rgba(8, 12, 20, .65-.85)` for fixed shell.
- Type: Inter for UI (`300–800`), JetBrains Mono for telemetry/log data.
- Text: `#fcfcfc` primary, `#a1a5b7` secondary, `#5e6278` muted.
- Semantic accents: blue `#3e97ff`, green `#50cd89`, red `#f1416c`, gold
  `#f1bc00`, purple `#7239ea`.
- Shape: 8px–16px rounded cards; 12px nav controls; soft black elevation.
- Layout: 75px nav rail, 65px sticky header, desktop analytical grid that
  collapses below 1200px and to one column below 768px.

## Raw source: base tokens

```css
:root {
  --bg-color: #0c0d14;
  --panel-bg: #151621;
  --card-bg: #151621;
  --border-color: rgba(255, 255, 255, 0.05);
  --text-primary: #fcfcfc;
  --text-secondary: #a1a5b7;
  --text-muted: #5e6278;
  --neon-green: #50cd89;
  --neon-red: #f1416c;
  --neon-blue: #3e97ff;
  --neon-gold: #f1bc00;
  --neon-purple: #7239ea;
  --shadow-sm: 0 4px 15px rgba(0, 0, 0, 0.25);
  --shadow-lg: 0 12px 30px rgba(0, 0, 0, 0.45);
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
  --transition-speed: 0.18s;
}
```

Full theme source: `backend/app/static/styles.css`.
