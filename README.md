# YouTube Transcript Internal Service

Small FastAPI service for fetching YouTube captions, caching them, and serving them through a web UI and token-protected API.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
# edit API_TOKEN; DATABASE_URL can be left unset for local SQLite
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`, enter the shared token, and fetch a transcript by YouTube URL or video ID.

## API

All API requests require:

```http
Authorization: Bearer <API_TOKEN>
```

Fetch transcript:

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  "http://127.0.0.1:8000/api/transcripts/dQw4w9WgXcQ?languages=en"
```

If `languages` is omitted, the service first tries `DEFAULT_LANGUAGES`, then falls back to the first transcript language YouTube provides for that video.

List available transcript languages:

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  "http://127.0.0.1:8000/api/transcripts/dQw4w9WgXcQ/languages"
```

Fetch a formatted transcript:

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  "http://127.0.0.1:8000/api/transcripts/dQw4w9WgXcQ/formats/srt?languages=en"
```

Formats: `json`, `text`, `srt`, `vtt`.

## Railway deployment

1. Push this directory to a GitHub repository.
2. In Railway, create a new project and choose **Deploy from GitHub repo**.
3. Add a Railway Postgres database to the same project.
4. In the API service variables, set:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}`
   - `API_TOKEN=<long-random-secret>`
   - `DEFAULT_LANGUAGES=en`
   - `ALLOWED_ORIGINS=https://your-site.com,https://another-site.com` if browser apps will call the API directly
5. Generate a public domain from the API service Networking settings.
6. Deploy. Railway uses `railway.toml` to run `uvicorn` and health-check `/healthz`.

## Calling from other sites

Backend-to-backend usage:

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  -H "X-Client-Name: marketing-site" \
  "https://your-service.up.railway.app/api/transcripts/dQw4w9WgXcQ"
```

Browser usage is also supported when `ALLOWED_ORIGINS` contains the calling site origin.

API calls are logged in the database without storing tokens. View recent calls:

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  "https://your-service.up.railway.app/api/usage?limit=100"
```

## Notes

This service uses YouTube captions only. It does not download audio or run speech-to-text. Some cloud IPs can be blocked by YouTube; in that case the service returns `youtube_blocked` while still serving already cached transcripts.
