"""Tests for tools/vision_tools.py — URL validation, type hints, error logging."""

import json
import logging
import os
from pathlib import Path
from typing import Awaitable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.vision_tools import (
    _validate_image_url,
    _handle_vision_analyze,
    _determine_mime_type,
    _image_to_base64_data_url,
    _resize_image_for_vision,
    _is_image_size_error,
    _MAX_BASE64_BYTES,
    _RESIZE_TARGET_BYTES,
    vision_analyze_tool,
    check_vision_requirements,
)


# ---------------------------------------------------------------------------
# _validate_image_url — urlparse-based validation
# ---------------------------------------------------------------------------


class TestValidateImageUrl:
    """Tests for URL validation, including urlparse-based netloc check."""

    def test_valid_https_url(self):
        with patch("tools.url_safety.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            assert _validate_image_url("https://example.com/image.jpg") is True

    def test_valid_http_url(self):
        with patch("tools.url_safety.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            assert _validate_image_url("http://cdn.example.org/photo.png") is True

    def test_valid_url_without_extension(self):
        """CDN endpoints that redirect to images should still pass."""
        with patch("tools.url_safety.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            assert _validate_image_url("https://cdn.example.com/abcdef123") is True

    def test_valid_url_with_query_params(self):
        with patch("tools.url_safety.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            assert _validate_image_url("https://img.example.com/pic?w=200&h=200") is True

    def test_localhost_url_blocked_by_ssrf(self):
        """localhost URLs are now blocked by SSRF protection."""
        assert _validate_image_url("http://localhost:8080/image.png") is False

    def test_valid_url_with_port(self):
        with patch("tools.url_safety.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            assert _validate_image_url("http://example.com:8080/image.png") is True

    def test_valid_url_with_path_only(self):
        with patch("tools.url_safety.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]):
            assert _validate_image_url("https://example.com/") is True

    def test_rejects_empty_string(self):
        assert _validate_image_url("") is False

    def test_rejects_none(self):
        assert _validate_image_url(None) is False

    def test_rejects_non_string(self):
        assert _validate_image_url(12345) is False

    def test_rejects_ftp_scheme(self):
        assert _validate_image_url("ftp://files.example.com/image.jpg") is False

    def test_rejects_file_scheme(self):
        assert _validate_image_url("file:///etc/passwd") is False

    def test_rejects_no_scheme(self):
        assert _validate_image_url("example.com/image.jpg") is False

    def test_rejects_javascript_scheme(self):
        assert _validate_image_url("javascript:alert(1)") is False

    def test_rejects_http_without_netloc(self):
        """http:// alone has no network location — urlparse catches this."""
        assert _validate_image_url("http://") is False

    def test_rejects_https_without_netloc(self):
        assert _validate_image_url("https://") is False

    def test_rejects_http_colon_only(self):
        assert _validate_image_url("http:") is False

    def test_rejects_data_url(self):
        assert _validate_image_url("data:image/png;base64,iVBOR") is False

    def test_rejects_whitespace_only(self):
        assert _validate_image_url("   ") is False

    def test_rejects_boolean(self):
        assert _validate_image_url(True) is False

    def test_rejects_list(self):
        assert _validate_image_url(["https://example.com"]) is False


# ---------------------------------------------------------------------------
# _determine_mime_type
# ---------------------------------------------------------------------------


class TestDetermineMimeType:
    def test_jpg(self):
        assert _determine_mime_type(Path("photo.jpg")) == "image/jpeg"

    def test_jpeg(self):
        assert _determine_mime_type(Path("photo.jpeg")) == "image/jpeg"

    def test_png(self):
        assert _determine_mime_type(Path("screenshot.png")) == "image/png"

    def test_gif(self):
        assert _determine_mime_type(Path("anim.gif")) == "image/gif"

    def test_webp(self):
        assert _determine_mime_type(Path("modern.webp")) == "image/webp"

    def test_unknown_extension_defaults_to_jpeg(self):
        assert _determine_mime_type(Path("file.xyz")) == "image/jpeg"


# ---------------------------------------------------------------------------
# _image_to_base64_data_url
# ---------------------------------------------------------------------------


class TestImageToBase64DataUrl:
    def test_returns_data_url(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        result = _image_to_base64_data_url(img)
        assert result.startswith("data:image/png;base64,")

    def test_custom_mime_type(self, tmp_path):
        img = tmp_path / "test.bin"
        img.write_bytes(b"\x00" * 16)
        result = _image_to_base64_data_url(img, mime_type="image/webp")
        assert result.startswith("data:image/webp;base64,")

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _image_to_base64_data_url(tmp_path / "nonexistent.png")


# ---------------------------------------------------------------------------
# _handle_vision_analyze — type signature & behavior
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Error logging with exc_info — verify tracebacks are logged
# ---------------------------------------------------------------------------


class TestErrorLoggingExcInfo:
    """Verify that exc_info=True is used in error/warning log calls."""

    @pytest.mark.asyncio
    async def test_download_failure_logs_exc_info(self, tmp_path, caplog):
        """After max retries, the download error should include exc_info."""
        from tools.vision_tools import _download_image

        with patch("tools.vision_tools.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=ConnectionError("network down"))
            mock_client_cls.return_value = mock_client

            dest = tmp_path / "image.jpg"
            with (
                caplog.at_level(logging.ERROR, logger="tools.vision_tools"),
                pytest.raises(ConnectionError),
            ):
                await _download_image(
                    "https://example.com/img.jpg", dest, max_retries=1
                )

            # Should have logged with exc_info (traceback present)
            error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert len(error_records) >= 1
            assert error_records[0].exc_info is not None








# ---------------------------------------------------------------------------
# check_vision_requirements
# ---------------------------------------------------------------------------


class TestVisionRequirements:
    def test_check_requirements_returns_bool(self):
        result = check_vision_requirements()
        assert isinstance(result, bool)

    def test_check_requirements_accepts_codex_auth(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MARLOW_HOME", str(tmp_path))
        (tmp_path / "auth.json").write_text(
            '{"active_provider":"openai-codex","providers":{"openai-codex":{"tokens":{"access_token":"codex-access-token","refresh_token":"codex-refresh-token"}}}}'
        )
        # config.yaml must reference the codex provider so vision auto-detect
        # falls back to the active provider via _read_main_provider().
        (tmp_path / "config.yaml").write_text(
            'model:\n  default: gpt-4o\n  provider: openai-codex\n'
        )
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        assert check_vision_requirements() is True


# ---------------------------------------------------------------------------
# Integration: registry entry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tilde expansion in local file paths
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# file:// URI support
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Base64 size pre-flight check
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Error classification for 400 responses
# ---------------------------------------------------------------------------




class TestVisionRegistration:
    def test_vision_analyze_registered(self):
        from tools.registry import registry

        entry = registry._tools.get("vision_analyze")
        assert entry is not None
        assert entry.toolset == "vision"
        assert entry.is_async is True

    def test_schema_has_required_fields(self):
        from tools.registry import registry

        entry = registry._tools.get("vision_analyze")
        schema = entry.schema
        assert schema["name"] == "vision_analyze"
        params = schema.get("parameters", {})
        props = params.get("properties", {})
        assert "image_url" in props
        assert "question" in props

    def test_handler_is_callable(self):
        from tools.registry import registry

        entry = registry._tools.get("vision_analyze")
        assert callable(entry.handler)


# ---------------------------------------------------------------------------
# _resize_image_for_vision — auto-resize oversized images
# ---------------------------------------------------------------------------


class TestResizeImageForVision:
    """Tests for the auto-resize function."""

    def test_small_image_returned_as_is(self, tmp_path):
        """Images under the limit should be returned unchanged."""
        # Create a small 10x10 red PNG
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        img = Image.new("RGB", (10, 10), (255, 0, 0))
        path = tmp_path / "small.png"
        img.save(path, "PNG")

        result = _resize_image_for_vision(path, mime_type="image/png")
        assert result.startswith("data:image/png;base64,")
        assert len(result) < _MAX_BASE64_BYTES

    def test_large_image_is_resized(self, tmp_path):
        """Images over the default target should be auto-resized to fit."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        # Create a large image that will exceed 5 MB in base64
        # A 4000x4000 uncompressed PNG will be large
        img = Image.new("RGB", (4000, 4000), (128, 200, 50))
        path = tmp_path / "large.png"
        img.save(path, "PNG")

        result = _resize_image_for_vision(path, mime_type="image/png")
        assert result.startswith("data:image/png;base64,")
        # Default target is _RESIZE_TARGET_BYTES (5 MB), not _MAX_BASE64_BYTES (20 MB)
        assert len(result) <= _RESIZE_TARGET_BYTES

    def test_custom_max_bytes(self, tmp_path):
        """The max_base64_bytes parameter should be respected."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        img = Image.new("RGB", (200, 200), (0, 128, 255))
        path = tmp_path / "medium.png"
        img.save(path, "PNG")

        # Set a very low limit to force resizing
        result = _resize_image_for_vision(path, max_base64_bytes=500)
        # Should still return a valid data URL
        assert result.startswith("data:image/")

    def test_jpeg_output_for_non_png(self, tmp_path):
        """Non-PNG images should be resized as JPEG."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        img = Image.new("RGB", (2000, 2000), (255, 128, 0))
        path = tmp_path / "photo.jpg"
        img.save(path, "JPEG", quality=95)

        result = _resize_image_for_vision(path, mime_type="image/jpeg",
                                           max_base64_bytes=50_000)
        assert result.startswith("data:image/jpeg;base64,")

    def test_constants_sane(self):
        """Hard limit should be larger than resize target."""
        assert _MAX_BASE64_BYTES == 20 * 1024 * 1024
        assert _RESIZE_TARGET_BYTES == 5 * 1024 * 1024
        assert _MAX_BASE64_BYTES > _RESIZE_TARGET_BYTES

    def test_extreme_aspect_ratio_preserved(self, tmp_path):
        """Extreme aspect ratios should be preserved during resize."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        # Very wide panorama: 8000x200
        img = Image.new("RGB", (8000, 200), (100, 150, 200))
        path = tmp_path / "panorama.png"
        img.save(path, "PNG")

        result = _resize_image_for_vision(path, mime_type="image/png",
                                           max_base64_bytes=50_000)
        assert result.startswith("data:image/")
        # Decode and check aspect ratio is roughly preserved
        import base64
        header, b64data = result.split(",", 1)
        raw = base64.b64decode(b64data)
        from io import BytesIO
        resized = Image.open(BytesIO(raw))
        original_ratio = 8000 / 200  # 40:1
        resized_ratio = resized.width / resized.height if resized.height > 0 else 0
        # Allow some tolerance (floor clamping), but ratio should stay above 10:1
        # With independent halving, ratio would collapse to ~1:1. Proportional
        # scaling should keep it well above 10.
        assert resized_ratio > 10, (
            f"Aspect ratio collapsed: {resized.width}x{resized.height} "
            f"(ratio {resized_ratio:.1f}, expected >10)"
        )

    def test_tall_narrow_image_preserved(self, tmp_path):
        """Tall narrow images should also preserve aspect ratio."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        # Very tall: 200x6000
        img = Image.new("RGB", (200, 6000), (200, 100, 50))
        path = tmp_path / "tall.png"
        img.save(path, "PNG")

        result = _resize_image_for_vision(path, mime_type="image/png",
                                           max_base64_bytes=50_000)
        assert result.startswith("data:image/")
        import base64
        from io import BytesIO
        header, b64data = result.split(",", 1)
        raw = base64.b64decode(b64data)
        resized = Image.open(BytesIO(raw))
        original_ratio = 6000 / 200  # 30:1 (h/w)
        resized_ratio = resized.height / resized.width if resized.width > 0 else 0
        assert resized_ratio > 5, (
            f"Aspect ratio collapsed: {resized.width}x{resized.height} "
            f"(h/w ratio {resized_ratio:.1f}, expected >5)"
        )

    def test_no_pillow_returns_original(self, tmp_path):
        """Without Pillow, oversized images should be returned as-is."""
        # Create a dummy file
        path = tmp_path / "test.png"
        # Write enough bytes to exceed a tiny limit
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000)

        with patch("tools.vision_tools._image_to_base64_data_url") as mock_b64:
            # Simulate a large base64 result
            mock_b64.return_value = "data:image/png;base64," + "A" * 200
            with patch.dict("sys.modules", {"PIL": None, "PIL.Image": None}):
                result = _resize_image_for_vision(path, max_base64_bytes=100)
                # Should return the original (oversized) data url
                assert len(result) > 100


# ---------------------------------------------------------------------------
# _is_image_size_error — detect size-related API errors
# ---------------------------------------------------------------------------


class TestIsImageSizeError:
    """Tests for the size-error detection helper."""

    def test_too_large_message(self):
        assert _is_image_size_error(Exception("Request payload too large"))

    def test_413_status(self):
        assert _is_image_size_error(Exception("HTTP 413 Payload Too Large"))

    def test_invalid_request(self):
        assert _is_image_size_error(Exception("invalid_request_error: image too big"))

    def test_exceeds_limit(self):
        assert _is_image_size_error(Exception("Image exceeds maximum size"))

    def test_unrelated_error(self):
        assert not _is_image_size_error(Exception("Connection refused"))

    def test_auth_error(self):
        assert not _is_image_size_error(Exception("401 Unauthorized"))

    def test_empty_message(self):
        assert not _is_image_size_error(Exception(""))


class TestDownloadRetryClassification:
    """Error-class-aware retry: 4xx fail-fast, 429/5xx/transient retried (issue #32296)."""

    @staticmethod
    def _status_error(status_code):
        import httpx

        request = httpx.Request("GET", "https://example.com/img.jpg")
        response = httpx.Response(status_code, request=request)
        return httpx.HTTPStatusError(
            f"{status_code}", request=request, response=response
        )

    def _make_client_raising_status(self, status_code):
        """AsyncClient whose response.raise_for_status() raises HTTPStatusError."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=self._status_error(status_code)
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        return mock_client

    def test_is_retryable_classification(self):
        from tools.vision_tools import _is_retryable_download_error

        # Non-retryable client errors
        for code in (400, 403, 404, 410):
            assert _is_retryable_download_error(self._status_error(code)) is False
        # Retryable: rate limit + server errors
        for code in (429, 500, 502, 503):
            assert _is_retryable_download_error(self._status_error(code)) is True
        # Policy/SSRF/size errors are terminal
        assert _is_retryable_download_error(PermissionError("blocked")) is False
        assert _is_retryable_download_error(ValueError("too large")) is False
        # Unclassified (network blip) is retryable
        assert _is_retryable_download_error(ConnectionError("reset")) is True

    @pytest.mark.asyncio
    async def test_404_fails_fast_without_retry(self, tmp_path):
        """A 404 must raise on the first attempt — no backoff sleep, no extra GETs."""
        import httpx
        from tools.vision_tools import _download_image

        mock_client = self._make_client_raising_status(404)
        with (
            patch("tools.vision_tools.httpx.AsyncClient", return_value=mock_client),
            patch("tools.vision_tools.check_website_access", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(httpx.HTTPStatusError),
        ):
            await _download_image(
                "https://example.com/missing.jpg", tmp_path / "x.jpg", max_retries=3
            )
        # Exactly one attempt, zero backoff sleeps.
        assert mock_client.get.await_count == 1
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_503_retries_then_raises(self, tmp_path):
        """A 5xx is retried up to max_retries, sleeping between attempts."""
        import httpx
        from tools.vision_tools import _download_image

        mock_client = self._make_client_raising_status(503)
        with (
            patch("tools.vision_tools.httpx.AsyncClient", return_value=mock_client),
            patch("tools.vision_tools.check_website_access", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(httpx.HTTPStatusError),
        ):
            await _download_image(
                "https://example.com/flaky.jpg", tmp_path / "y.jpg", max_retries=3
            )
        # All three attempts used, two backoff sleeps between them.
        assert mock_client.get.await_count == 3
        assert mock_sleep.await_count == 2


class TestNativeImageContract:
    @pytest.mark.asyncio
    async def test_handler_passes_question_and_pixels_without_model_call(self, tmp_path):
        image = tmp_path / "native.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        result = await _handle_vision_analyze({"image_url": str(image), "question": "Inspect"})
        assert result["_multimodal"]
        assert "Inspect" in result["content"][0]["text"]
        assert result["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_helper_rejects_auxiliary_model_selection(self, tmp_path):
        result = json.loads(await vision_analyze_tool(str(tmp_path / "image.png"), "Inspect", "other-model"))
        assert not result["success"] and "overrides are retired" in result["error"]

    @pytest.mark.asyncio
    async def test_non_image_never_enters_model_context(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP_SECRET")
        result = json.loads(await vision_analyze_tool(str(secret), "Extract"))
        assert not result["success"] and "Only real image files" in result["error"]

    @pytest.mark.asyncio
    async def test_blocked_url_never_downloads(self):
        with patch("tools.vision_tools._validate_image_url", return_value=True), patch("tools.vision_tools.check_website_access", return_value={"message": "blocked"}), patch("tools.vision_tools._download_image", new_callable=AsyncMock) as download:
            result = json.loads(await vision_analyze_tool("https://blocked.example/image.png", "Inspect"))
        assert not result["success"]
        download.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tilde_and_file_uri_inputs_keep_native_pixels(self, tmp_path, monkeypatch):
        image = tmp_path / "native.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        monkeypatch.setenv("HOME", str(tmp_path))
        for name in ("~/native.png", "file://" + str(image)):
            result = await vision_analyze_tool(name, "Inspect")
            assert result["_multimodal"]

    @pytest.mark.asyncio
    async def test_native_image_obeys_disclosure_scope(self, tmp_path):
        from agent.codex_file_scope import file_scope
        image = tmp_path / "secret.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        with file_scope([tmp_path / "authorized"]):
            result = json.loads(await vision_analyze_tool(str(image), "Inspect"))
        assert not result["success"] and "scope" in result["error"]
