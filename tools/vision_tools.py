#!/usr/bin/env python3
"""
Vision Tools Module

This module provides vision analysis tools that work with image URLs.
Returns native image content for the active Codex model; no auxiliary inference.

Available tools:
- vision_analyze_tool: Analyze images from URLs with custom prompts

Features:
- Downloads images from URLs and converts to base64 for API compatibility
- Comprehensive image description
- Context-aware analysis based on user queries
- Automatic temporary file cleanup
- Proper error handling and validation
- Debug logging support

Usage:
    from vision_tools import vision_analyze_tool
    import asyncio

    # Analyze an image
    result = await vision_analyze_tool(
        image_url="https://example.com/image.jpg",
        user_prompt="What architectural style is this building?"
    )
"""

import base64
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Dict, Optional
from urllib.parse import urlparse
import httpx
from marlow_constants import get_marlow_dir
from tools.debug_helpers import DebugSession
from tools.website_policy import check_website_access
import sys

logger = logging.getLogger(__name__)

_debug = DebugSession("vision_tools", env_var="VISION_TOOLS_DEBUG")

# HTTP download timeout is independent of Codex's model request timeout.
def _resolve_download_timeout() -> float:
    env_val = os.getenv("MARLOW_VISION_DOWNLOAD_TIMEOUT", "").strip()
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    try:
        from marlow_cli.config import cfg_get, load_config
        cfg = load_config()
        val = cfg_get(cfg, "tools", "vision", "download_timeout")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return 30.0

_VISION_DOWNLOAD_TIMEOUT = _resolve_download_timeout()

# Hard cap on downloaded image file size (50 MB). Prevents OOM from
# attacker-hosted multi-gigabyte files or decompression bombs.
_VISION_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _validate_image_url(url: str) -> bool:
    """
    Basic validation of image URL format.

    Args:
        url (str): The URL to validate

    Returns:
        bool: True if URL appears to be valid, False otherwise
    """
    if not url or not isinstance(url, str):
        return False

    # Basic HTTP/HTTPS URL check
    if not url.startswith(("http://", "https://")):
        return False

    # Parse to ensure we at least have a network location; still allow URLs
    # without file extensions (e.g. CDN endpoints that redirect to images).
    parsed = urlparse(url)
    if not parsed.netloc:
        return False

    # Block private/internal addresses to prevent SSRF
    from tools.url_safety import is_safe_url
    if not is_safe_url(url):
        return False

    return True


def _detect_image_mime_type(image_path: Path) -> Optional[str]:
    """Return a MIME type when the file looks like a supported image."""
    with image_path.open("rb") as f:
        header = f.read(64)

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if image_path.suffix.lower() == ".svg":
        head = image_path.read_text(encoding="utf-8", errors="ignore")[:4096].lower()
        if "<svg" in head:
            return "image/svg+xml"
    return None


def _is_retryable_download_error(error: Exception) -> bool:
    """Return True only for transient image-download failures worth retrying.

    Non-retryable (fail-fast):
      - httpx.HTTPStatusError with a 4xx status other than 429 (404/403/410/...):
        the resource is missing or forbidden; retrying can't change that.
      - PermissionError: blocked by website policy / SSRF guard.
      - ValueError: image too large or blocked redirect — deterministic.

    Retryable (transient):
      - httpx 429 (rate limited) and 5xx (server-side) errors.
      - Connection/timeout/transport errors (httpx.TransportError) and any
        other unclassified exception, which may be a flaky network blip.
    """
    if isinstance(error, (PermissionError, ValueError)):
        return False
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if 400 <= status < 500 and status != 429:
            return False
        return True
    return True


async def _download_image(image_url: str, destination: Path, max_retries: int = 3) -> Path:
    """
    Download an image from a URL to a local destination (async) with retry logic.

    Args:
        image_url (str): The URL of the image to download
        destination (Path): The path where the image should be saved
        max_retries (int): Maximum number of retry attempts (default: 3)

    Returns:
        Path: The path to the downloaded image

    Raises:
        Exception: If download fails after all retries
    """
    import asyncio

    # Create parent directories if they don't exist
    destination.parent.mkdir(parents=True, exist_ok=True)

    async def _ssrf_redirect_guard(response):
        """Re-validate each redirect target to prevent redirect-based SSRF.

        Without this, an attacker can host a public URL that 302-redirects
        to http://169.254.169.254/ and bypass the pre-flight is_safe_url check.

        Must be async because httpx.AsyncClient awaits event hooks.
        """
        if response.is_redirect and response.next_request:
            redirect_url = str(response.next_request.url)
            from tools.url_safety import is_safe_url
            if not is_safe_url(redirect_url):
                raise ValueError(
                    f"Blocked redirect to private/internal address: {redirect_url}"
                )

    last_error = None
    for attempt in range(max_retries):
        try:
            blocked = check_website_access(image_url)
            if blocked:
                raise PermissionError(blocked["message"])

            # Download the image with appropriate headers using async httpx
            # Enable follow_redirects to handle image CDNs that redirect (e.g., Imgur, Picsum)
            # SSRF: event_hooks validates each redirect target against private IP ranges
            async with httpx.AsyncClient(
                timeout=_VISION_DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]},
            ) as client:
                response = await client.get(
                    image_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "image/*,*/*;q=0.8",
                    },
                )
                response.raise_for_status()

                # Reject overly large images early via Content-Length header.
                cl = response.headers.get("content-length")
                if cl and int(cl) > _VISION_MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Image too large ({int(cl)} bytes, max {_VISION_MAX_DOWNLOAD_BYTES})"
                    )

                final_url = str(response.url)
                blocked = check_website_access(final_url)
                if blocked:
                    raise PermissionError(blocked["message"])

                # Save the image content (double-check actual size)
                body = response.content
                if len(body) > _VISION_MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Image too large ({len(body)} bytes, max {_VISION_MAX_DOWNLOAD_BYTES})"
                    )
                destination.write_bytes(body)

            return destination
        except Exception as e:
            last_error = e
            # Error-class-aware retry: only retry transient failures. A 4xx
            # client error (404/403/410, etc.) will never succeed on retry —
            # the resource isn't there or we're not allowed — so burning 3
            # attempts with 2s/4s/8s backoff just inflates latency. 429 (rate
            # limit) and 5xx remain retryable. PermissionError (policy block)
            # and ValueError (too-large / SSRF redirect) are also terminal.
            if not _is_retryable_download_error(e) or attempt >= max_retries - 1:
                logger.error(
                    "Image download failed after %s attempt(s): %s",
                    attempt + 1,
                    str(e)[:100],
                    exc_info=True,
                )
                raise
            wait_time = 2 ** (attempt + 1)  # 2s, 4s, 8s
            logger.warning("Image download failed (attempt %s/%s): %s", attempt + 1, max_retries, str(e)[:50])
            logger.warning("Retrying in %ss...", wait_time)
            await asyncio.sleep(wait_time)

    # The loop always returns on success or re-raises on the final/non-retryable
    # attempt, so reaching here means max_retries was non-positive.
    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"_download_image exited retry loop without attempting (max_retries={max_retries})"
    )


def _determine_mime_type(image_path: Path) -> str:
    """
    Determine the MIME type of an image based on its file extension.

    Args:
        image_path (Path): Path to the image file

    Returns:
        str: The MIME type (defaults to image/jpeg if unknown)
    """
    extension = image_path.suffix.lower()
    mime_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.bmp': 'image/bmp',
        '.webp': 'image/webp',
        '.svg': 'image/svg+xml'
    }
    return mime_types.get(extension, 'image/jpeg')


def _image_to_base64_data_url(image_path: Path, mime_type: Optional[str] = None) -> str:
    """
    Convert an image file to a base64-encoded data URL.

    Args:
        image_path (Path): Path to the image file
        mime_type (Optional[str]): MIME type of the image (auto-detected if None)

    Returns:
        str: Base64-encoded data URL (e.g., "data:image/jpeg;base64,...")
    """
    # Read the image as bytes
    data = image_path.read_bytes()

    # Encode to base64
    encoded = base64.b64encode(data).decode("ascii")

    # Determine MIME type
    mime = mime_type or _determine_mime_type(image_path)

    # Create data URL
    data_url = f"data:{mime};base64,{encoded}"

    return data_url


# Absolute hard ceiling for vision API payloads (20 MB) — above this, no major
# provider accepts the image and we reject outright.
_MAX_BASE64_BYTES = 20 * 1024 * 1024

# Proactive embed cap (4 MB).  This is the size we resize an image DOWN to
# before embedding it into conversation history, regardless of the 20 MB hard
# ceiling.  Anthropic's per-image base64 limit is 5 MB; once an oversized image
# is baked into history (e.g. a vision tool-result), it is re-sent on every
# subsequent turn and permanently wedges the session with a 400 that retries
# can't clear (the bad bytes are immutable history).  Capping at embed time —
# with headroom under 5 MB — is the only durable fix.  Matches the post-failure
# shrink target in agent.conversation_compression so behaviour is consistent
# whether we resize proactively or reactively.
_EMBED_TARGET_BYTES = 4 * 1024 * 1024

# Target size when auto-resizing on API failure (5 MB).  After a provider
# rejects an image, we downscale to this target and retry once.
_RESIZE_TARGET_BYTES = 5 * 1024 * 1024


def _is_image_size_error(error: Exception) -> bool:
    """Detect if an API error is related to image or payload size."""
    err_str = str(error).lower()
    return any(hint in err_str for hint in (
        "too large", "payload", "413", "content_too_large",
        "request_too_large", "image_url", "invalid_request",
        "exceeds", "size limit",
    ))


def _resize_image_for_vision(image_path: Path, mime_type: Optional[str] = None,
                              max_base64_bytes: int = _RESIZE_TARGET_BYTES) -> str:
    """Convert an image to a base64 data URL, auto-resizing if too large.

    Tries Pillow first to progressively downscale oversized images.  If Pillow
    is not installed or resizing still exceeds the limit, falls back to the raw
    bytes and lets the caller handle the size check.

    Returns the base64 data URL string.
    """
    # Quick file-size estimate: base64 expands by ~4/3, plus data URL header.
    # Skip the expensive full-read + encode if Pillow can resize directly.
    file_size = image_path.stat().st_size
    estimated_b64 = (file_size * 4) // 3 + 100  # ~header overhead
    if estimated_b64 <= max_base64_bytes:
        # Small enough — just encode directly.
        data_url = _image_to_base64_data_url(image_path, mime_type=mime_type)
        if len(data_url) <= max_base64_bytes:
            return data_url
    else:
        data_url = None  # defer full encode; try Pillow resize first

    # Attempt auto-resize with Pillow (soft dependency)
    try:
        from PIL import Image
        import io as _io
    except ImportError:
        logger.info("Pillow not installed — cannot auto-resize oversized image")
        if data_url is None:
            data_url = _image_to_base64_data_url(image_path, mime_type=mime_type)
        return data_url  # caller will raise the size error

    logger.info("Image file is %.1f MB (estimated base64 %.1f MB, limit %.1f MB), auto-resizing...",
                file_size / (1024 * 1024), estimated_b64 / (1024 * 1024),
                max_base64_bytes / (1024 * 1024))

    mime = mime_type or _determine_mime_type(image_path)
    # Choose output format: JPEG for photos (smaller), PNG for transparency
    pil_format = "PNG" if mime == "image/png" else "JPEG"
    out_mime = "image/png" if pil_format == "PNG" else "image/jpeg"

    try:
        img = Image.open(image_path)
    except Exception as exc:
        logger.info("Pillow cannot open image for resizing: %s", exc)
        if data_url is None:
            data_url = _image_to_base64_data_url(image_path, mime_type=mime_type)
        return data_url  # fall through to size-check in caller
    # Convert RGBA to RGB for JPEG output
    if pil_format == "JPEG" and img.mode in {"RGBA", "P"}:
        img = img.convert("RGB")

    # Strategy: halve dimensions until base64 fits, up to 4 rounds.
    # For JPEG, also try reducing quality at each size step.
    # For PNG, quality is irrelevant — only dimension reduction helps.
    quality_steps = (85, 70, 50) if pil_format == "JPEG" else (None,)
    prev_dims = (img.width, img.height)
    candidate = None  # will be set on first loop iteration

    for attempt in range(5):
        if attempt > 0:
            # Proportional scaling: halve the longer side and scale the
            # shorter side to preserve aspect ratio (min dimension 64).
            scale = 0.5
            new_w = max(int(img.width * scale), 64)
            new_h = max(int(img.height * scale), 64)
            # Re-derive the scale from whichever dimension hit the floor
            # so both axes shrink by the same factor.
            if new_w == 64 and img.width > 0:
                effective_scale = 64 / img.width
                new_h = max(int(img.height * effective_scale), 64)
            elif new_h == 64 and img.height > 0:
                effective_scale = 64 / img.height
                new_w = max(int(img.width * effective_scale), 64)
            # Stop if dimensions can't shrink further
            if (new_w, new_h) == prev_dims:
                break
            img = img.resize((new_w, new_h), Image.LANCZOS)
            prev_dims = (new_w, new_h)
            logger.info("Resized to %dx%d (attempt %d)", new_w, new_h, attempt)

        for q in quality_steps:
            buf = _io.BytesIO()
            save_kwargs = {"format": pil_format}
            if q is not None:
                save_kwargs["quality"] = q
            img.save(buf, **save_kwargs)
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            candidate = f"data:{out_mime};base64,{encoded}"
            if len(candidate) <= max_base64_bytes:
                logger.info("Auto-resized image fits: %.1f MB (quality=%s, %dx%d)",
                            len(candidate) / (1024 * 1024), q,
                            img.width, img.height)
                return candidate

    # If we still can't get it small enough, return the best attempt
    # and let the caller decide
    if candidate is not None:
        logger.warning("Auto-resize could not fit image under %.1f MB (best: %.1f MB)",
                       max_base64_bytes / (1024 * 1024), len(candidate) / (1024 * 1024))
        return candidate

    # Shouldn't reach here, but fall back to full encode
    return data_url or _image_to_base64_data_url(image_path, mime_type=mime_type)


# ---------------------------------------------------------------------------
# Native fast path: short-circuit the auxiliary LLM when the active main model
# supports native vision. Instead of asking a separate LLM to describe the
# image and returning text, we load the image, base64-encode it, and return a
# multimodal tool-result envelope. The agent loop unwraps the envelope into an
# OpenAI-style content list on the `tool` role; provider adapters (anthropic,
# codex_responses, chat_completions) translate that into Anthropic
# tool_result image blocks / Responses input_image / OpenAI image_url tool
# content. The main model then "sees" the pixels directly on its next turn.
# ---------------------------------------------------------------------------


def _supports_media_in_tool_results(provider: str, model: str) -> bool:
    """Whether the given provider+model combination accepts image content
    inside a tool-result message.

    Codex Responses and retained OpenAI-compatible local endpoints both accept
    the normalized multimodal result shape. Unknown plugin providers fall back
    to the auxiliary vision text path.
    """
    if not isinstance(provider, str):
        return False
    p = provider.strip().lower()
    if not p:
        return False

    return p in {"openai-codex", "custom", "local", "lmstudio", "ollama"}


def _should_use_native_vision_fast_path(*args, **kwargs):
    return True


def _build_native_vision_tool_result(
    image_url: str,
    question: str,
    image_data_url: str,
    image_size_bytes: int,
) -> Dict[str, Any]:
    """Build the multimodal tool-result envelope returned by the fast path.

    Shape:
      {
        "_multimodal": True,
        "content": [
          {"type": "text", "text": "<short note + the user's question>"},
          {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ],
        "text_summary": "<plain-text fallback>",
        "meta": {"image_url": ..., "size_bytes": N},
      }

    The text part exists for two reasons: (1) it gives the model an
    instruction to act on now that the pixels are in context, and
    (2) providers that don't support multimodal tool results can fall back
    to ``text_summary``.
    """
    # The tool-result text part is intentionally minimal. The model already
    # has the user's original question in context; this just acknowledges
    # the image is now visible and reminds it what it was asked.
    text_part = (
        "Image loaded into your context — you can see it natively now. "
        "Use your built-in vision to answer the user."
    )
    if isinstance(question, str) and question.strip():
        text_part += f"\n\nQuestion: {question.strip()}"

    summary = (
        f"Image attached natively for the main model "
        f"({image_size_bytes / 1024:.1f} KB). "
        "Answer using built-in vision."
    )

    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": text_part},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
        "text_summary": summary,
        "meta": {
            "image_url": image_url[:200],
            "size_bytes": image_size_bytes,
            "native_vision": True,
        },
    }


async def _vision_analyze_native(
    image_url: str,
    question: str,
) -> Any:
    """Fast path for vision-capable main models.

    Loads the image (local file OR remote URL), base64-encodes it, and
    returns a multimodal tool-result envelope. The agent loop unwraps it;
    provider adapters serialize it into the right tool-result-with-image
    shape for each backend.

    Returns:
        A ``_multimodal`` envelope dict on success.
        A JSON error string on failure (matches the existing tool-result
        contract so the agent loop displays errors normally).
    """
    if not isinstance(image_url, str) or not image_url.strip():
        return tool_error("image_url is required", success=False)

    temp_image_path: Optional[Path] = None
    should_cleanup = False
    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)

        # Resolve the image source (mirrors vision_analyze_tool's logic
        # exactly so behaviour is consistent).
        resolved_url = image_url
        if resolved_url.startswith("file://"):
            resolved_url = resolved_url[len("file://"):]
        local_path = Path(os.path.expanduser(resolved_url))

        if local_path.is_file():
            from agent.codex_file_scope import permits_delivery
            if permits_delivery(local_path.resolve()) is False:
                raise PermissionError("Image is outside the trusted file disclosure scope")
            temp_image_path = local_path
            should_cleanup = False
        elif _validate_image_url(image_url):
            blocked = check_website_access(image_url)
            if blocked:
                return tool_error(blocked["message"], success=False)
            temp_dir = get_marlow_dir("cache/vision", "temp_vision_images")
            temp_image_path = temp_dir / f"temp_image_{uuid.uuid4()}.jpg"
            await _download_image(image_url, temp_image_path)
            should_cleanup = True
        else:
            return tool_error(
                "Invalid image source. Provide an HTTP/HTTPS URL or a "
                "valid local file path.",
                success=False,
            )

        image_size_bytes = temp_image_path.stat().st_size
        detected_mime_type = _detect_image_mime_type(temp_image_path)
        if not detected_mime_type:
            return tool_error(
                "Only real image files are supported for vision analysis.",
                success=False,
            )

        image_data_url = _image_to_base64_data_url(
            temp_image_path, mime_type=detected_mime_type,
        )

        # Proactive embed cap: this image gets baked into conversation
        # history and re-sent on every subsequent turn.  Anthropic rejects
        # any single base64 image over 5 MB with a 400, and because history
        # is immutable, an oversized embed permanently wedges the session —
        # retries can't clear bytes that are already in the request.  Resize
        # DOWN to the embed target (4 MB, headroom under 5 MB) whenever the
        # payload exceeds it, not just at the 20 MB hard ceiling.
        if len(image_data_url) > _EMBED_TARGET_BYTES:
            image_data_url = _resize_image_for_vision(
                temp_image_path, mime_type=detected_mime_type,
                max_base64_bytes=_EMBED_TARGET_BYTES,
            )
            # If even resizing can't get under the absolute hard ceiling,
            # there's nothing more we can do — reject rather than embed a
            # session-wedging payload.
            if len(image_data_url) > _MAX_BASE64_BYTES:
                return tool_error(
                    f"Image too large for vision API: base64 payload is "
                    f"{len(image_data_url) / (1024 * 1024):.1f} MB "
                    f"(limit {_MAX_BASE64_BYTES / (1024 * 1024):.0f} MB) "
                    f"even after resizing. Install Pillow "
                    f"(`pip install Pillow`) for better auto-resize, "
                    f"or compress the image manually.",
                    success=False,
                )

        return _build_native_vision_tool_result(
            image_url=image_url,
            question=question,
            image_data_url=image_data_url,
            image_size_bytes=image_size_bytes,
        )

    except Exception as exc:
        logger.warning("Native vision fast path failed: %s", exc, exc_info=True)
        return tool_error(f"Native vision failed: {exc}", success=False)
    finally:
        # Only delete temp files we created — never user-provided paths.
        if should_cleanup and temp_image_path is not None:
            try:
                if temp_image_path.exists():
                    temp_image_path.unlink()
            except Exception:
                pass


async def vision_analyze_tool(image_url: str, user_prompt: str, model: str = None):
    """Return image pixels to the sole Codex turn; never invoke a helper model."""
    if model:
        return tool_error("Vision model overrides are retired; configure the profile Codex model", success=False)
    return await _vision_analyze_native(image_url, user_prompt)


def check_vision_requirements():
    return True



# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

VISION_ANALYZE_SCHEMA = {
    "name": "vision_analyze",
    "description": (
        "Load an image into the conversation so you can see it. Accepts a "
        "URL, local file path, or data URL. When your active model has "
        "native vision, the image is attached to your context directly "
        "and you read the pixels yourself on the next turn — call this "
        "any time the user references an image (filepath in their message, "
        "URL in tool output, screenshot from the browser, etc.). For "
        "models without image support, the configured Codex provider reports "
        "an explicit capability error; no helper model is called."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "image_url": {
                "type": "string",
                "description": "Image URL (http/https), local file path, or data: URL to load."
            },
            "question": {
                "type": "string",
                "description": "Your specific question or request about the image. Optional context the model uses on the next turn after seeing the image."
            }
        },
        "required": ["image_url", "question"]
    }
}


def _handle_vision_analyze(args: Dict[str, Any], **kw: Any) -> Awaitable[str]:
    image_url = args.get("image_url", "")
    question = args.get("question", "")

    return _vision_analyze_native(image_url, question)


registry.register(
    name="vision_analyze",
    toolset="vision",
    schema=VISION_ANALYZE_SCHEMA,
    handler=_handle_vision_analyze,
    check_fn=check_vision_requirements,
    is_async=True,
    emoji="👁️",
)
