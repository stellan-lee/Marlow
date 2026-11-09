"""Keep ordinary audio attachments and retire automatic voice processing."""

from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner.session_store = None
    runner._is_user_authorized = lambda _source: True
    runner._handle_message_with_agent = AsyncMock()
    return runner


def _event(message_type, text="", path=None):
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="1", chat_type="dm", user_id="2"
        ),
        media_urls=[path] if path else [],
        media_types=["audio/ogg"] if path else [],
    )


@pytest.mark.asyncio
async def test_voice_note_replies_with_text_instruction_without_starting_agent():
    runner = _make_runner()
    event = _event(MessageType.VOICE, path="/tmp/voice.ogg")

    assert await runner._handle_message(event) == "Please send your message as text."
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_caption_proceeds_without_voice_attachment():
    runner = _make_runner()
    event = _event(MessageType.VOICE, "Please check this.", path="/tmp/voice.ogg")
    result = await runner._prepare_inbound_message_text(
        event=event, source=event.source, history=[]
    )

    assert result == "Please check this."


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", [MessageType.AUDIO, MessageType.DOCUMENT])
async def test_audio_attachment_keeps_file_path_and_caption(message_type, monkeypatch):
    runner = _make_runner()
    event = _event(message_type, "An example track.", path="/tmp/cache_123_my_song.ogg")
    monkeypatch.setattr(runner, "_stage_runtime_attachment", lambda source, path: path)

    result = await runner._prepare_inbound_message_text(
        event=event, source=event.source, history=[]
    )

    assert "audio file attachment: 'my_song.ogg'" in result
    assert "/tmp/cache_123_my_song.ogg" in result
    assert result.endswith("An example track.")


@pytest.mark.asyncio
async def test_mixed_photo_and_audio_attachment_route_independently(monkeypatch):
    runner = _make_runner()
    event = _event(MessageType.PHOTO, "Here are two files.")
    event.media_urls = ["/tmp/photo.png", "/tmp/track.ogg"]
    event.media_types = ["image/png", "audio/ogg"]
    monkeypatch.setattr(runner, "_stage_runtime_attachment", lambda source, path: path)

    result = await runner._prepare_inbound_message_text(
        event=event, source=event.source, history=[]
    )

    assert list(runner._pending_native_image_paths_by_session.values()) == [["/tmp/photo.png"]]
    assert "/tmp/track.ogg" in result
    assert "Here are two files." in result


def test_gateway_ignores_retired_stt_config():
    config = GatewayConfig.from_dict({"stt": {"enabled": True}, "stt_enabled": True})

    assert "stt" not in config.to_dict()
    assert "stt_enabled" not in config.to_dict()
