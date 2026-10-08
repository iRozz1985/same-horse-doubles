# Same-Horse Doubles

A site that lists **same-horse doubles**: a horse that runs on today's card AND
also appears in a future ante-post feature race. For each one it prices the
"win today **and** win the feature" double using the independent model, and
shows margin-added quotes at 25%, 50% and 75%.

It rebuilds **twice a day (09:00 and 12:00 UK)** on GitHub's servers and
publishes the result to GitHub Pages, so you just open a URL.

## How it works

| Piece | What it does |
|-------|--------------|
| `build_doubles_page.py` | Discovers ante-post markets, scans today's cards, matches the same horse across both, prices the double, and writes `index.html`. |
| `contingency_pricer.py` | The pricing model (independent double + margin). |
| `racing_query.py` | Fetches today's races from the Ladbrokes feed. |
| `lads_client.py` | Ladbrokes API client. Reads the key from the `LADS_API_KEY` environment variable. |
| `.github/workflows/update-doubles.yml` | Runs the builder at 09:00 and 12:00 UK and commits the page. |

The Ladbrokes API needs a private key, so this can't run purely in a browser
(the key would be exposed, and the browser can't call that API directly).
Instead, the key is stored as an encrypted **GitHub Secret** and only used by
the scheduled build job on GitHub's servers. It is never committed.

## One-time setup

### 1. Create the repo and upload these files
Create a new repository and upload everything in this folder, **including the
hidden `.github` folder**. If the hidden folder is awkward to upload, create the
workflow file directly in GitHub's web editor: **Add file -> Create new file**,
name it `.github/workflows/update-doubles.yml`, and paste the contents.

### 2. Add your Ladbrokes API key as a secret
1. In the repo: **Settings -> Secrets and variables -> Actions**.
2. **New repository secret**.
3. Name: `LADS_API_KEY`
4. Value: your Ladbrokes API key.
5. **Add secret**.

### 3. Enable GitHub Pages
1. **Settings -> Pages**.
2. Source: **Deploy from a branch**, branch **main**, folder **/ (root)**, **Save**.
3. The live URL appears after a minute: `https://<username>.github.io/<repo>/`.

### 4. Build the first page
1. **Actions** tab -> **Update same-horse doubles** -> **Run workflow**.
2. It takes a few minutes (it scans the whole day's card). When it finishes
   (green tick), reload your Pages URL.

After that it rebuilds automatically at 09:00 and 12:00 UK.

## Running locally (optional)

```bash
pip install -r requirements.txt
set LADS_API_KEY=your_key_here       # PowerShell:  $env:LADS_API_KEY="..."
python build_doubles_page.py
# faster test, fewer countries:
python build_doubles_page.py --countries UK,IRE
```

Then open `index.html`.

## Notes

- The scan is heavy (it discovers ante-post markets and scans every race in the
  chosen countries), so a run takes a few minutes. That's fine on GitHub's
  servers.
- Matching is by **exact** horse name (case/spacing ignored). A horse listed
  under slightly different names in the two markets won't be paired.
- "True price" is the fair, zero-margin double. The 25/50/75% columns are
  margin-added quotes.
- The tool depends on the structure of the Ladbrokes feed; if that feed changes,
  the tool may need updating.
