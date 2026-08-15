# NBC Sports Rotoworld NFL Player News RSS

Creates two files from:

`https://www.nbcsports.com/fantasy/football/player-news`

- `feed.xml` — RSS 2.0 feed
- `news.csv` — structured CSV

CSV columns:

`Player Name, Team Initials, Position, Headline, News Snippet, Source, Rotoworld Author, Date, URL`

## Set it up on GitHub

1. Create a new **public** GitHub repository.
2. Upload all files from this project, including the `.github` folder.
3. Open the repository's **Actions** tab and enable workflows if GitHub asks.
4. Run **Update Rotoworld feed** once manually.
5. In **Settings → Pages**, set:
   - Source: **Deploy from a branch**
   - Branch: `main`
   - Folder: `/ (root)`
6. After GitHub Pages publishes, your URLs will look like:

   `https://YOUR-USERNAME.github.io/YOUR-REPO/feed.xml`

   `https://YOUR-USERNAME.github.io/YOUR-REPO/news.csv`

The workflow is scheduled every 15 minutes. GitHub Actions schedules can run a little late.

## Run locally

```bash
python -m pip install -r requirements.txt
python scraper.py
```

## What the scraper captures

Each Rotoworld item is normalized into:

- Player Name
- Team Initials
- Position
- Headline
- News Snippet
- Source
- Rotoworld Author
- Date
- URL

The date is taken from the individual NBC player-news URL when available.

## De-duplication

The script loads the existing `news.csv`, merges newly scraped stories into it, and de-duplicates by URL. It keeps the latest 100 items by default.

Change this line in `scraper.py` if you want a larger archive:

```python
MAX_ITEMS = 100
```

## Important

This is an unofficial personal feed generator. NBC can change its page markup at any time, which may require updating the parser. Use it in accordance with NBC's terms and applicable rules.
