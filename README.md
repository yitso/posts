# Posts

Published writing by Yitso. The site is built by [Itso](https://github.com/yitso/itso)
and deployed to [GitHub Pages](https://yitso.github.io/posts/).

## Content

Articles live in `content/YYYY/MM/DD/`. YAML front matter must include a stable,
unique `id` containing lowercase letters, numbers, and dashes, plus `title`,
`date` (`YYYY-MM-DD`), and `summary`. The public article URL is
`https://yitso.github.io/posts/posts/<id>/`; changing its ID breaks that URL.

Use public HTTPS URLs for article images for now: the current engine does not
copy content-repository attachments into its output.

## Build and deployment

`configs/site.json` configures this site. `.github/workflows/site.yml` builds
pull requests and every push to `main`, then checks generated pages and local
asset links. Only `main` can deploy to the `github-pages` environment.
GitHub Pages must use **GitHub Actions** as its source.

The engine is pinned to `1c488a84f58aa2bf0326dec8b358b790e6bc2a97`; update this reference and the
workflow's `uses` line together when adopting a reviewed engine revision.

To reproduce the build, check out the pinned Itso revision beside this repository
as `itso`, install its `requirements.txt` into a virtual environment, and run:

```sh
cd ../itso
npm ci
SITE_BASE=/posts/ VITE_BASE=/posts/static/itso/dist/ npm run build:assets
CONTENT_ROOT=../posts SITE_BASE=/posts/ STATIC_SITE_DIR=dist python -m app.build --static --skip-assets --site-config configs/site.json
python ../posts/scripts/check_site.py dist
```

## License

The contents of this repository are licensed under the
[Creative Commons Attribution-NonCommercial 4.0 International License](https://creativecommons.org/licenses/by-nc/4.0/).
You may use and share them for **non-commercial purposes** with proper attribution.
For commercial use, please contact me.
