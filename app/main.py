from __future__ import annotations

from contextlib import asynccontextmanager
from os import getenv
from sys import stderr
from time import perf_counter
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from starlette.responses import RedirectResponse

from app.auth import require_api_token
from app.config import Settings, get_settings
from app.database import Base, make_engine, make_session_factory, session_scope
from app.formatters import FORMATTERS
from app.models import ApiRequestLog
from app.schemas import ErrorResponse
from app.service import get_or_fetch_transcript, get_uncached_transcript
from app.youtube import (
    TranscriptServiceError,
    _get_cookies_file,
    _make_cookie_session,
    _normalize_cookies_content,
    extract_video_id,
    is_explicit_language_request,
    list_available_transcripts,
    normalize_languages,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            Base.metadata.create_all(bind=engine)
            app.state.database_ready = True
        except SQLAlchemyError as exc:
            app.state.database_ready = False
            print(f"Database setup failed during startup: {exc}", file=stderr)
        yield

    app = FastAPI(
        title="YouTube Transcript Internal Service",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Client-Name"],
        )

    @app.middleware("http")
    async def log_api_requests(request: Request, call_next):
        started_at = perf_counter()
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            duration_ms = int((perf_counter() - started_at) * 1000)
            try:
                with session_scope(request.app.state.session_factory) as session:
                    session.add(
                        ApiRequestLog(
                            method=request.method,
                            path=request.url.path,
                            query=request.url.query,
                            client_name=request.headers.get("x-client-name"),
                            client_host=request.client.host if request.client else None,
                            user_agent=request.headers.get("user-agent"),
                            status_code=response.status_code,
                            duration_ms=duration_ms,
                        )
                    )
            except SQLAlchemyError as exc:
                print(f"API request logging failed: {exc}", file=stderr)
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/debug/cookies", dependencies=[Depends(require_api_token)])
    def debug_cookies() -> dict:
        raw = getenv("YOUTUBE_COOKIES") or ""
        normalized = _normalize_cookies_content(raw.strip())
        cookie_count = 0
        cookie_names: list[str] = []
        for line in normalized.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookie_count += 1
                cookie_names.append(parts[5])  # name field
        session = _make_cookie_session()
        cookies_file = _get_cookies_file()
        import os as _os
        return {
            "env_var_set": bool(raw),
            "env_var_length": len(raw),
            "has_real_newlines": "\n" in raw,
            "has_literal_backslash_n": "\\n" in raw,
            "normalized_length": len(normalized),
            "cookies_parsed": cookie_count,
            "cookie_names": cookie_names,
            "session_cookies": len(session.cookies) if session else 0,
            "cookies_file": cookies_file,
            "cookies_file_size": _os.path.getsize(cookies_file) if cookies_file and _os.path.exists(cookies_file) else 0,
        }

    @app.get("/debug/fetch/{video_id}", dependencies=[Depends(require_api_token)])
    def debug_fetch(video_id: str) -> dict:
        from app.youtube import (
            _list_transcripts,
            _fetch_yt_dlp_payload,
            _fetch_segments,
            _segment_to_dict,
            YouTubeBlocked,
            TranscriptUnavailable,
        )
        result: dict = {}

        # Test primary API path - list then fetch
        try:
            transcript_list = _list_transcripts(video_id)
            available = [{"code": t.language_code, "generated": t.is_generated} for t in transcript_list]
            result["primary_api_list"] = "success"
            result["primary_api_transcripts"] = available

            # Try to actually fetch the first available transcript
            for t in transcript_list:
                try:
                    raw = _fetch_segments(t)
                    segments = [_segment_to_dict(s) for s in raw]
                    result["primary_api_fetch"] = "success"
                    result["primary_api_fetch_lang"] = t.language_code
                    result["primary_api_fetch_segments"] = len(segments)
                    break
                except Exception as e:
                    result["primary_api_fetch"] = "error"
                    result["primary_api_fetch_error"] = f"{type(e).__name__}: {e}"
                    break
        except YouTubeBlocked as e:
            result["primary_api_list"] = "youtube_blocked"
            result["primary_api_error"] = str(e)
        except TranscriptUnavailable as e:
            result["primary_api_list"] = "transcript_unavailable"
            result["primary_api_error"] = str(e)
        except Exception as e:
            result["primary_api_list"] = "error"
            result["primary_api_error"] = f"{type(e).__name__}: {e}"

        # Test yt-dlp direct (no proxy)
        try:
            fetched = _fetch_yt_dlp_payload(video_id, ["en"], True, None)
            result["ytdlp_direct"] = "success"
            result["ytdlp_direct_segments"] = len(fetched.segments)
            result["ytdlp_direct_lang"] = fetched.language_code
        except YouTubeBlocked as e:
            result["ytdlp_direct"] = "youtube_blocked"
            result["ytdlp_direct_error"] = str(e)
        except TranscriptUnavailable as e:
            result["ytdlp_direct"] = "transcript_unavailable"
            result["ytdlp_direct_error"] = str(e)
        except Exception as e:
            result["ytdlp_direct"] = "error"
            result["ytdlp_direct_error"] = f"{type(e).__name__}: {e}"

        return result

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        is_authenticated = (
            (request.cookies.get("transcript_token") or "").strip() == settings.normalized_api_token
            and bool(settings.normalized_api_token)
        )
        login_error = "Access token did not match the configured API_TOKEN." if request.query_params.get("login_error") else ""
        return HTMLResponse(_render_page(is_authenticated=is_authenticated, error=login_error))

    @app.post("/login")
    def login(token: str = Form(...)) -> RedirectResponse:
        submitted_token = token.strip()
        if submitted_token == settings.normalized_api_token and settings.normalized_api_token:
            response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie("transcript_token", submitted_token, httponly=True, samesite="lax")
        else:
            response = RedirectResponse("/?login_error=1", status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie("transcript_token", "", max_age=0)
        return response

    @app.post("/logout")
    def logout() -> RedirectResponse:
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie("transcript_token")
        return response

    @app.get("/api/config/status")
    def config_status() -> dict[str, bool]:
        return {
            "api_token_configured": bool(settings.normalized_api_token),
            "database_url_configured": bool(settings.database_url),
            "database_ready": bool(getattr(app.state, "database_ready", False)),
            "proxy_configured": bool(
                getenv("PROXY_URL")
                or (getenv("WEBSHARE_PROXY_USERNAME") and getenv("WEBSHARE_PROXY_PASSWORD"))
            ),
        }

    @app.get("/api/transcripts/{video_id}", dependencies=[Depends(require_api_token)])
    def transcript(video_id: str, request: Request, languages: str | None = None):
        return _handle_transcript(request.app.state.session_factory, settings, video_id, languages)

    @app.get("/api/transcripts/{video_id}/languages", dependencies=[Depends(require_api_token)])
    def transcript_languages(video_id: str):
        try:
            parsed_video_id = extract_video_id(video_id)
            return {
                "video_id": parsed_video_id,
                "languages": [language.__dict__ for language in list_available_transcripts(parsed_video_id)],
            }
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_video_id", "message": str(exc)},
            ) from exc
        except TranscriptServiceError as exc:
            status_code = status.HTTP_424_FAILED_DEPENDENCY if exc.error == "youtube_blocked" else status.HTTP_404_NOT_FOUND
            raise HTTPException(
                status_code=status_code,
                detail=ErrorResponse(error=exc.error, message=exc.message, video_id=exc.video_id).model_dump(),
            ) from exc

    @app.get("/api/transcripts/{video_id}/formats/{output_format}", dependencies=[Depends(require_api_token)])
    def transcript_format(video_id: str, output_format: str, request: Request, languages: str | None = None):
        if output_format not in FORMATTERS:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "format_not_supported",
                    "message": "Supported formats are json, text, srt, and vtt.",
                },
            )
        transcript_response = _handle_transcript(request.app.state.session_factory, settings, video_id, languages)
        media_type, formatter = FORMATTERS[output_format]
        return Response(content=formatter(transcript_response), media_type=media_type)

    @app.get("/api/usage", dependencies=[Depends(require_api_token)])
    def api_usage(request: Request, limit: int = 100):
        capped_limit = min(max(limit, 1), 500)
        with session_scope(request.app.state.session_factory) as session:
            rows = (
                session.query(ApiRequestLog)
                .order_by(ApiRequestLog.created_at.desc())
                .limit(capped_limit)
                .all()
            )
            return {
                "logs": [
                    {
                        "method": row.method,
                        "path": row.path,
                        "query": row.query,
                        "client_name": row.client_name,
                        "client_host": row.client_host,
                        "status_code": row.status_code,
                        "duration_ms": row.duration_ms,
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in rows
                ]
            }

    @app.post("/web/transcripts", dependencies=[Depends(require_api_token)])
    def web_transcript(request: Request, video: str = Form(...), languages: str | None = Form(default=None)):
        try:
            video_id = extract_video_id(video)
            transcript_response = _handle_transcript(request.app.state.session_factory, settings, video_id, languages)
            return HTMLResponse(
                _render_page(
                    is_authenticated=True,
                    video=video,
                    languages=languages or "",
                    transcript_segments=[
                        segment.model_dump() for segment in transcript_response.segments
                    ],
                    metadata=(
                        f"{transcript_response.video_id} | {transcript_response.language_code} | "
                        f"{'auto-generated' if transcript_response.is_generated else 'manual'} | "
                        f"{transcript_response.source}"
                    ),
                )
            )
        except (ValueError, TranscriptServiceError) as exc:
            return HTMLResponse(
                _render_page(
                    is_authenticated=True,
                    video=video,
                    languages=languages or "",
                    error=getattr(exc, "message", str(exc)),
                ),
                status_code=400,
            )
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict):
                message = str(detail.get("message") or detail.get("error") or "Unable to fetch transcript.")
            else:
                message = str(detail)
            return HTMLResponse(
                _render_page(
                    is_authenticated=True,
                    video=video,
                    languages=languages or "",
                    error=message,
                ),
                status_code=exc.status_code,
            )

    @app.post("/web/languages", dependencies=[Depends(require_api_token)])
    def web_languages(video: str = Form(...)):
        try:
            video_id = extract_video_id(video)
            languages = list_available_transcripts(video_id)
            return HTMLResponse(
                _render_page(
                    is_authenticated=True,
                    video=video,
                    available_languages=[language.__dict__ for language in languages],
                )
            )
        except (ValueError, TranscriptServiceError) as exc:
            return HTMLResponse(
                _render_page(
                    is_authenticated=True,
                    video=video,
                    error=getattr(exc, "message", str(exc)),
                ),
                status_code=400,
            )

    return app


def _handle_transcript(
    session_factory: sessionmaker,
    settings: Settings,
    video_id_or_url: str,
    languages: str | None,
):
    try:
        video_id = extract_video_id(video_id_or_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_video_id", "message": str(exc)},
        ) from exc

    explicit_languages = is_explicit_language_request(languages)
    language_priority = normalize_languages(languages, settings.language_priority)
    try:
        with session_scope(session_factory) as session:
            return get_or_fetch_transcript(
                session,
                video_id,
                language_priority,
                allow_any_language=not explicit_languages,
            )
    except SQLAlchemyError as exc:
        print(f"Transcript cache unavailable, fetching without cache: {exc}", file=stderr)
        return get_uncached_transcript(
            video_id,
            language_priority,
            allow_any_language=not explicit_languages,
        )
    except TranscriptServiceError as exc:
        status_code = status.HTTP_424_FAILED_DEPENDENCY if exc.error == "youtube_blocked" else status.HTTP_404_NOT_FOUND
        raise HTTPException(
            status_code=status_code,
            detail=ErrorResponse(error=exc.error, message=exc.message, video_id=exc.video_id).model_dump(),
        ) from exc


def _render_page(
    *,
    is_authenticated: bool,
    video: str = "",
    languages: str = "",
    result: str = "",
    transcript_segments: list[dict] | None = None,
    metadata: str = "",
    error: str = "",
    available_languages: list[dict] | None = None,
) -> str:
    if not is_authenticated:
        login_error = f'<p class="error">{_escape_html(error)}</p>' if error else ""
        body = """
        <form action="/login" method="post" class="panel">
          <label>Access token
            <input type="password" name="token" autocomplete="current-password" required autofocus>
          </label>
          {login_error}
          <button type="submit">Continue</button>
        </form>
        """.format(login_error=login_error)
    else:
        body = f"""
        <form action="/logout" method="post" class="logout"><button type="submit">Sign out</button></form>
        <form action="/web/transcripts" method="post" class="panel">
          <label>YouTube URL or video ID
            <input name="video" value="{_escape_attr(video)}" placeholder="https://www.youtube.com/watch?v=..." required autofocus>
          </label>
          <label>Language priority
            <input name="languages" value="{_escape_attr(languages)}" placeholder="Auto, or enter en,hi">
          </label>
          <button type="submit">Fetch transcript</button>
        </form>
        <form action="/web/languages" method="post" class="secondary-panel">
          <input type="hidden" name="video" value="{_escape_attr(video)}">
          <button type="submit">Show available languages</button>
        </form>
        {_languages_html(video, available_languages)}
        {_result_html(result, transcript_segments, metadata, error)}
        """

    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>YouTube Transcript Service</title>
        <style>
          :root {{
            color-scheme: light;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #f6f7f9;
            color: #18202a;
          }}
          body {{ margin: 0; }}
          main {{ width: min(920px, calc(100vw - 32px)); margin: 40px auto; }}
          h1 {{ font-size: 28px; margin: 0 0 20px; letter-spacing: 0; }}
          .panel, .secondary-panel, .result {{
            background: #ffffff;
            border: 1px solid #dfe3e8;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
          }}
          .secondary-panel {{ margin-top: 12px; padding: 14px 20px; }}
          label {{ display: block; font-weight: 650; margin-bottom: 16px; }}
          input {{
            box-sizing: border-box;
            display: block;
            width: 100%;
            margin-top: 8px;
            padding: 11px 12px;
            border: 1px solid #bcc5d1;
            border-radius: 6px;
            font: inherit;
          }}
          button {{
            border: 0;
            border-radius: 6px;
            background: #1f6feb;
            color: white;
            font: inherit;
            font-weight: 700;
            padding: 11px 14px;
            cursor: pointer;
          }}
          .logout {{ text-align: right; margin-bottom: 12px; }}
          .logout button {{ background: #5c6672; }}
          .result {{ margin-top: 18px; }}
          .meta {{ color: #5c6672; margin: 0 0 12px; }}
          .error {{ color: #b42318; font-weight: 700; }}
          .language-list {{ display: grid; gap: 8px; margin-top: 12px; }}
          .language-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            border-top: 1px solid #e8ebef;
            padding-top: 8px;
          }}
          .language-row form {{ margin: 0; }}
          .language-row button {{ background: #285c3b; padding: 8px 10px; }}
          .transcript-list {{ display: grid; gap: 6px; }}
          .transcript-row {{
            display: grid;
            grid-template-columns: 72px 1fr;
            gap: 14px;
            align-items: start;
            padding: 8px 0;
            border-top: 1px solid #edf0f4;
          }}
          .timestamp {{
            color: #1f6feb;
            font-variant-numeric: tabular-nums;
            font-weight: 750;
            white-space: nowrap;
          }}
          .transcript-text {{ line-height: 1.55; overflow-wrap: anywhere; }}
          pre {{
            white-space: pre-wrap;
            overflow-wrap: anywhere;
            margin: 0;
            line-height: 1.55;
            font-size: 14px;
          }}
        </style>
      </head>
      <body>
        <main>
          <h1>YouTube Transcript Service</h1>
          {body}
        </main>
      </body>
    </html>
    """


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _escape_html(value: str) -> str:
    return _escape_attr(value).replace(">", "&gt;")


def _result_html(
    result: str,
    transcript_segments: list[dict] | None,
    metadata: str,
    error: str,
) -> str:
    if error:
        return f'<section class="result"><p class="error">{_escape_html(error)}</p></section>'
    if transcript_segments is not None:
        rows = []
        for segment in transcript_segments:
            rows.append(
                f"""
                <div class="transcript-row">
                  <span class="timestamp">{_format_web_timestamp(float(segment["start"]))}</span>
                  <span class="transcript-text">{_escape_html(str(segment["text"]))}</span>
                </div>
                """
            )
        return (
            f'<section class="result"><p class="meta">{_escape_html(metadata)}</p>'
            f'<div class="transcript-list">{"".join(rows)}</div></section>'
        )
    if result:
        return f'<section class="result"><p class="meta">{_escape_html(metadata)}</p><pre>{_escape_html(result)}</pre></section>'
    return ""


def _format_web_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes}:{secs:02}"


def _languages_html(video: str, languages: list[dict] | None) -> str:
    if languages is None:
        return ""
    if not languages:
        return '<section class="result"><p class="error">No transcript languages were found for this video.</p></section>'

    rows = []
    for language in languages:
        language_code = str(language["language_code"])
        label = f'{language["language"]} ({language_code})'
        kind = "auto-generated" if language["is_generated"] else "manual"
        rows.append(
            f"""
            <div class="language-row">
              <span>{_escape_html(label)} | {_escape_html(kind)}</span>
              <form action="/web/transcripts" method="post">
                <input type="hidden" name="video" value="{_escape_attr(video)}">
                <input type="hidden" name="languages" value="{_escape_attr(language_code)}">
                <button type="submit">Fetch</button>
              </form>
            </div>
            """
        )
    return f'<section class="result"><p class="meta">Available transcript languages</p><div class="language-list">{"".join(rows)}</div></section>'


app = create_app()
