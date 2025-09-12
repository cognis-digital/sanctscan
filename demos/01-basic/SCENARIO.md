# Demo 01 -- Basic sanctions name-screening

This demo shows SANCTSCAN screening a small batch of customer names against a
tiny OFAC-style watchlist (`watchlist.csv`) using explainable fuzzy matching.

## Files

- `watchlist.csv` -- a sample sanctions list (uid, name, program, type, aliases).
- `customers.csv` -- names to screen (the `full_name` column).

## Run it

Screen a single, deliberately misspelled name:

```bash
python -m sanctscan screen \
  --watchlist demos/01-basic/watchlist.csv \
  --name "Vladmir Putin"
```

Screen the whole customer file and emit JSON for a CI gate:

```bash
python -m sanctscan screen \
  --watchlist demos/01-basic/watchlist.csv \
  --input demos/01-basic/customers.csv \
  --column full_name \
  --format json --threshold 0.85
```

## Expected result

- `"Vladmir Putin"` (a typo of "Vladimir Putin") **matches** the watchlist
  entry `Vladimir Putin` with a high score (~0.9+). The output explains which
  query token matched which watchlist token and at what ratio.
- `"Pjotr Iljitsch"` matches the alias of `Pyotr Ilyich` via transliteration
  drift once the threshold is relaxed.
- Clean names such as `"Jane Q. Public"` are reported as **CLEAR**.
- Because at least one name is flagged, the process exits with a **non-zero**
  status code (1) -- suitable for failing a compliance pipeline.
