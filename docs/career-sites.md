# Career sites

Many employers publish vacancies on their own career site that never reach
Platsbanken — large industrials, universities and fast-growing tech companies in
particular. Most of those sites run on a handful of applicant-tracking systems
that publish a public feed, and RoleLens ships one collector per platform.

A career-site posting is ranked, screened and judged exactly like a Platsbanken
ad. It costs nothing until it is selected for evaluation.

---

## Adding a site

Each entry in `config.json` → `career_sites` names a platform, a short unique
`name`, and the few values that platform needs:

```json
"career_sites": [
  {"platform": "teamtailor", "name": "omegapoint", "url": "https://omegapoint.teamtailor.com", "company": "Omegapoint"},
  {"platform": "greenhouse", "name": "mentimeter", "board": "mentimeter", "company": "Mentimeter"}
]
```

| Field | Meaning |
|---|---|
| `platform` | One of the platforms below. |
| `name` | Lowercase id (letters, digits, `.`, `_`, `-`). It prefixes every posting id, so keep it stable. |
| `company` | Employer name on the cards. Optional; the feed's own name is used otherwise. |
| `city`, `region` | Location for postings whose feed names none. |
| `country` | Country assumed when a posting's location names none. Default `Sverige`. |
| `enabled` | `false` keeps the entry in your config without reading it. |

`url` must be `https://`. Then check it without spending anything:

```bash
python3 ~/.local/scripts/rolelens.py --verbose fetch
```

Each site logs a line such as
`Career site omegapoint (teamtailor): 31 posting(s) read, 31 new or changed`.
A site that fails is logged, skipped, and named in an alert line; the other
sources still run.

---

## Platforms

### Teamtailor

Used by a large share of Swedish employers, on `*.teamtailor.com` or a custom
domain. Every Teamtailor site publishes `{url}/jobs.json` with every open job
and its full description.

```json
{"platform": "teamtailor", "name": "acme", "url": "https://acme.teamtailor.com"}
```

**Finding it:** open the careers page; if `{url}/jobs.json` returns JSON, it is
Teamtailor.

### Varbi

Used by Swedish universities, regions and agencies. RoleLens reads the public
RSS feed at `{url}/what:rssfeed/`. The feed names no city, so set one.

```json
{"platform": "varbi", "name": "uppsala-universitet", "url": "https://uu.varbi.com",
 "company": "Uppsala universitet", "city": "Uppsala", "region": "Uppsala län"}
```

Doctoral and student posts come through too; `exclude_student_roles` and the
ranking sort them out.

### Greenhouse

```json
{"platform": "greenhouse", "name": "mentimeter", "board": "mentimeter"}
```

**Finding it:** job links look like `job-boards.greenhouse.io/<board>/jobs/…`.

### Lever

```json
{"platform": "lever", "name": "acme", "board": "acme"}
```

**Finding it:** job links look like `jobs.lever.co/<board>/…`. Boards on
`jobs.eu.lever.co` need `"instance": "eu"`.

### Ashby

```json
{"platform": "ashby", "name": "acme", "board": "acme"}
```

**Finding it:** job links look like `jobs.ashbyhq.com/<board>/…`.

### SmartRecruiters

The listing carries no descriptions, so each new posting costs one detail
request. The listing is filtered server-side to one country.

```json
{"platform": "smartrecruiters", "name": "vattenfall", "board": "vattenfall", "country": "se"}
```

**Finding it:** job links look like `jobs.smartrecruiters.com/<board>/…`.
Optional: `max_pages` (100 postings per page, default 10).

### Workday

Large employers' Workday sites answer a JSON search. The listing carries no
descriptions, so each new posting costs one detail request.

```json
{"platform": "workday", "name": "acme", "url": "https://acme.wd3.myworkdayjobs.com",
 "tenant": "acme", "site": "External", "applied_facets": {"locationCountry": ["<id>"]}}
```

**Finding it:** a careers URL like
`https://acme.wd3.myworkdayjobs.com/en-US/External` gives `url` (scheme and
host), `site` (`External`) and usually `tenant` (the first label of the host).
The host number (`wd1`, `wd3`, `wd5`) matters.

**Filtering to Sweden:** filter by country on the careers page and copy the
`locationCountry=<id>` value from the address bar into `applied_facets`.
Without a facet a global board lists every country; RoleLens still drops foreign
postings, but pays a detail request for each one first. Optional: `search_text`,
`locale` (default `en-US`), `max_pages` (20 postings per page, default 25).

### SAP SuccessFactors

SuccessFactors career sites have no public JSON API but render the same markup
for every customer, so RoleLens reads the search page and each job page.

```json
{"platform": "successfactors", "name": "acme", "url": "https://jobs.acme.com"}
```

**Finding it:** job links look like `{url}/job/<City-Title-Slug>/<number>/`.
Optional: `location_search` (default `Sweden`; empty lists every country),
`max_pages` (default 10).

This collector reads HTML, so a heavily customised site can break it. A broken
site fails on its own and is named in the run's alert line.

---

## How postings are handled

- **Identity.** A posting's id is `<name>:<platform key>`, stored under source
  `career_site`. An edited posting has a new content hash and competes for
  evaluation again.
- **Descriptions first.** A posting without meaningful text is not stored. On
  SmartRecruiters, Workday and SuccessFactors, detail pages are fetched only
  for postings not yet stored, at most `career_site_max_details` per site per
  run, so a large board fills in over a few runs. Once stored, those postings
  are not re-read.
- **Duplicates.** A posting whose employer and title match a job already stored
  from Platsbanken is the same vacancy and is left out.
- **Country.** A structured country is used when the feed has one. Otherwise a
  location naming Sweden or a Swedish city is Swedish, one naming another
  country or a large foreign city is not, and anything else gets the site's
  `country` (default `Sverige`). Non-Swedish postings are filtered out before
  they are stored.
- **Politeness.** One site at a time, public endpoints only, over HTTPS, with
  `query_delay_ms` between detail requests and an identifying User-Agent. No
  logins, forms or access controls are ever touched. Check a site's terms before
  you add it.

---

## Adding a platform

A collector is one function in `rolelens.py`:

```python
def collect_example(http, site, settings, known) -> list[JobRecord]:
    ...
```

It reads the feed with `http.json_request` or `http.fetch`, builds each posting
with `career_site_job(...)` (which returns `None` for a posting without usable
text), and uses `known` — the platform keys already stored for this site — to
skip detail requests. Register it in `CAREER_SITE_COLLECTORS`, add its required
fields to `CAREER_SITE_REQUIRED`, and add a test with a recorded payload shape to
`CareerSiteCollectorTests`.
