# Peblo TV Mini

Peblo is a small content-management and publishing service for a TV catalogue. It pairs a React admin interface with a FastAPI backend that stores shows, episode language variants, artwork, publishing history, and a generated public catalogue.

## What it does

- Seeds a starter catalogue from `seed_shows.json` on first run.
- Manages shows and episodes, including language variants of the same content.
- Validates catalogue readiness before publishing: duration, artwork, section, and duplicate language-variant checks.
- Enforces artwork formats for posters, banners, and thumbnails.
- Publishes a static JSON catalogue and provides public browse and search endpoints.
- Records successful and blocked publish attempts.

## Architecture

| Component | Technology | Default address |
| --- | --- | --- |
| Web app | React, TypeScript, Vite, Nginx | http://localhost:5173 |
| API | FastAPI, SQLAlchemy | http://localhost:8000 |
| Database | PostgreSQL 16 | Docker-internal |
| Storage | Docker volume / local `data/storage` | Served by the API |

## Quick start with Docker

Prerequisites: Docker Desktop with Docker Compose.

```bash
docker compose up --build
```

Then open [http://localhost:5173](http://localhost:5173). The API health check is available at [http://localhost:8000/health](http://localhost:8000/health).

To stop the stack, press `Ctrl+C`, then run:

```bash
docker compose down
```

The Compose stack keeps Postgres data and uploaded artwork in named volumes. To completely reset the local Docker data and trigger seeding again:

```bash
docker compose down -v
```

## Local development

Prerequisites: Python 3.13+, Node.js 22+, and npm. The API defaults to SQLite locally, so Postgres is optional outside Docker.

### API

From the repository root:

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r api/requirements.txt
$env:PYTHONPATH = "api"
uvicorn app.main:app --reload --port 8000
```

The local database is created at `data/peblo.db` and uploaded artwork is saved beneath `data/storage` unless you override the environment variables below.

### Web app

In a second terminal:

```bash
cd web
npm install
npm run dev
```

The development server listens on http://localhost:5173 and calls the API at http://localhost:8000. In the Docker image, Nginx routes `/api` requests to the API container.

## Configuration

Copy `.env.example` if you want to customize the API configuration. Do not commit real credentials.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | SQLite locally; Postgres in Compose | SQLAlchemy connection URL |
| `STORAGE_DIR` | `./data/storage` | Generated catalogue and uploaded artwork |
| `ADMIN_TOKEN` | `admin-demo` | Token allowed to publish |
| `EDITOR_TOKEN` | `editor-demo` | Token allowed to manage content |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed web origins |

For authenticated admin calls, send the token in an authorization header:

```bash
curl -H "Authorization: Bearer editor-demo" http://localhost:8000/admin/shows
```

Use `admin-demo` (or your configured `ADMIN_TOKEN`) for publishing.

## API overview

### Public endpoints

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/health` | Liveness check |
| `GET` | `/catalog` | Most recently published catalogue JSON |
| `GET` | `/catalog/search` | Search published content |
| `GET` | `/storage/{path}` | Uploaded artwork |

`/catalog/search` accepts optional `q`, `category`, `language`, and `section` query parameters.

### Admin endpoints

All of these require a bearer token. Publishing specifically requires the admin token.

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/admin/shows` | List shows; filter with `q`, `section`, `status`, `language`, `page`, and `page_size` |
| `PUT` | `/admin/shows/{show_id}` | Update show metadata |
| `PUT` | `/admin/episodes/{episode_id}` | Update an episode or language variant |
| `POST` | `/admin/shows/{show_id}/artwork/{kind}` | Upload `poster`, `banner`, or `thumbnail` artwork |
| `GET` | `/admin/validation-report` | List conditions blocking publication |
| `GET` | `/admin/publish-runs` | View the 20 newest publishing attempts |
| `POST` | `/admin/catalog/publish` | Validate and publish the public catalogue |

Artwork uploads must be PNG, JPEG, or WebP, no larger than 200 KB, and approximately the following dimensions:

| Kind | Required dimensions | Aspect ratio |
| --- | --- | --- |
| Poster | 600 x 900 | 2:3 |
| Banner | 1280 x 720 | 16:9 |
| Thumbnail | 640 x 360 | 16:9 |

## Publishing workflow

1. Edit show and episode information through the admin interface or API.
2. Upload all three artwork kinds for each published show.
3. Request `GET /admin/validation-report` with an editor/admin token.
4. Resolve every reported issue.
5. Request `POST /admin/catalog/publish` with the admin token.
6. Read the generated catalogue at `GET /catalog`.

The seed data intentionally contains validation examples, so the first publish can be blocked until those records are corrected.

## Tests and checks

Run the backend tests:

```bash
$env:PYTHONPATH = "api"
pytest api/tests
```

Run the web type-check and production build:

```bash
cd web
npm install
npm run build
```

The GitHub Actions workflow runs the backend tests, web build, and Docker image build for pushes and pull requests.

## Repository layout

```text
api/                 FastAPI application, dependencies, and tests
web/                 React/Vite frontend and Nginx production image
seed_shows.json      Initial catalogue data, loaded only into an empty database
reference.json       Product/reference data supplied with the project
docker-compose.yml   Full local stack: Postgres, API, and web app
.env.example         Environment-variable template
```

## Engineering notes

### Atomic publishing and viewer reads

Publishing builds the full catalogue before it is exposed. `write_catalog()` writes JSON to a UUID-named temporary file in the storage directory, then replaces `catalogue.json` with `Path.replace()`. On the local/POSIX filesystem this rename is atomic: viewers receive either the previous complete catalogue or the new complete catalogue, never a partial JSON document. If the process stops before the replace, the live catalogue is unaffected; the temporary file is orphaned. Cleaning up such temporary files is a known gap.

The viewer serves this pre-published file rather than querying the CMS database on each request. That isolates catalogue reads from database availability and load, gives every reader a consistent snapshot, and is easy to cache at a CDN. The trade-offs are deliberate staleness until the next publish, no per-request personalization, and a single file that will eventually become too large to load wholesale.

### Search and storage

Search reloads the published JSON and linearly scans its shows and grouped episodes for every request; the optional query, category, language, and section filters compose with AND logic. This is suitable for a few thousand records, but repeated parsing and O(n) scans become noticeable at tens of thousands of episodes or high concurrent traffic. The next step would be an in-memory catalogue cache plus a dedicated index such as PostgreSQL full-text search, Meilisearch, or Typesense.

Storage is currently local, path-based file access in `api/app/main.py`; it is not yet a fully swappable storage class. Moving to Cloudflare R2 would mean introducing a small `Storage` interface (`read`, `write`, `exists`, `url`) and a local-disk implementation, then adding an R2/S3 implementation behind that interface. The public artwork URLs would become R2/CDN URLs while catalogue publishing would use an object upload/copy strategy that preserves atomic versioning.

### Scope, operations, and delivery

This submission intentionally leaves out show/episode creation and deletion in the CMS, Alembic migrations, per-episode artwork, a production authentication provider, and temporary-file cleanup. They were kept out to focus the exercise on reliable publishing, editable validation blockers, and the core viewer workflow. AI assistance was used to accelerate implementation and documentation; its suggestions were accepted only after checking them against the existing API, tests, and build output, and rejected when they conflicted with the supplied constraints.

The primary operational alert would be **time since last successful publish** (and, secondarily, publish failure rate). A stale kids catalogue can look healthy at `/health` while quietly serving old content, so freshness is the more meaningful signal.

Approximate effort: Part A — 2 hours; Part B — 2 hours; Part C — 1.5 hours; Part D — 1 hour; Part E — 1 hour. These estimates include implementation, manual validation, and documentation.
