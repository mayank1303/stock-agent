// Static definitions shown in the help panel (info icon in header).
// Simpler and more reliable than trying to auto-detect and wrap terms
// inside dynamically-rendered markdown - a fixed reference panel avoids
// hacking react-markdown's rendering internals for a "nice to have".
export const GLOSSARY = [
  { term: "P/E Ratio", def: "Price-to-Earnings ratio. Current price divided by earnings per share. Higher generally means the market expects more future growth (or the stock is expensive relative to current profit)." },
  { term: "Market Cap", def: "Total value of all shares outstanding (price × number of shares). A rough measure of company size." },
  { term: "Dividend Yield", def: "Annual dividend paid per share, as a % of the current share price." },
  { term: "52-Week High/Low", def: "The highest and lowest price the stock has traded at in the last 52 weeks (1 year) - the standard trader reference range." },
  { term: "YTD", def: "Year-to-date: the % change from January 1st of the current year to now." },
  { term: "All-Time High/Low", def: "The highest/lowest price ever recorded for this stock, across its entire history (can be decades old and adjusted for stock splits)." },
];