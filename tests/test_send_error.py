"""Errors reach only the person who ran the command, including after a
public defer, where Discord would otherwise show the error to the channel."""
from __future__ import annotations

from types import SimpleNamespace

import discord

from lib.utils import ValidationError, handle_command_error, send_error


class _Flags:
    def __init__(self, loading: bool, ephemeral: bool):
        self.loading = loading
        self.ephemeral = ephemeral


class FakeInteraction:
    def __init__(self, *, done: bool, rtype=None, loading=True, ephemeral=False):
        self.calls: list = []
        outer = self

        class Response:
            type = rtype

            def is_done(self):
                return done

            async def send_message(self, content=None, **kw):
                outer.calls.append(("response", content, kw))

        class Followup:
            async def send(self, content=None, **kw):
                outer.calls.append(("followup", content, kw))

        self.response = Response()
        self.followup = Followup()
        self.command = SimpleNamespace(name="test")
        self._original = SimpleNamespace(flags=_Flags(loading, ephemeral))

    async def original_response(self):
        return self._original

    async def delete_original_response(self):
        self.calls.append(("delete", None, {}))


PUBLIC_DEFER = discord.InteractionResponseType.deferred_channel_message


async def test_not_yet_responded_uses_ephemeral_response():
    inter = FakeInteraction(done=False)
    await send_error(inter, "❌ nope")
    assert inter.calls == [("response", "❌ nope", {"ephemeral": True})]


async def test_public_placeholder_removed_before_private_followup():
    inter = FakeInteraction(done=True, rtype=PUBLIC_DEFER, loading=True, ephemeral=False)
    await send_error(inter, "❌ nope")
    assert [c[0] for c in inter.calls] == ["delete", "followup"]
    assert inter.calls[1][2] == {"ephemeral": True}


async def test_private_defer_left_alone():
    inter = FakeInteraction(done=True, rtype=PUBLIC_DEFER, loading=True, ephemeral=True)
    await send_error(inter, "❌ nope")
    assert [c[0] for c in inter.calls] == ["followup"]


async def test_response_with_content_is_never_deleted():
    # The command already posted something; only the error is added.
    inter = FakeInteraction(done=True, rtype=PUBLIC_DEFER, loading=False, ephemeral=False)
    await send_error(inter, "❌ nope")
    assert [c[0] for c in inter.calls] == ["followup"]


async def test_extra_kwargs_pass_through():
    inter = FakeInteraction(done=False)
    mentions = discord.AllowedMentions.none()
    await send_error(inter, "❌ nope", allowed_mentions=mentions)
    assert inter.calls[0][2]["allowed_mentions"] is mentions


async def test_handle_command_error_goes_private():
    inter = FakeInteraction(done=True, rtype=PUBLIC_DEFER, loading=True, ephemeral=False)
    await handle_command_error(inter, ValidationError("x", "Pass either `title:` or `user:`."))
    assert inter.calls[-1] == ("followup", "❌ Pass either `title:` or `user:`.", {"ephemeral": True})
    assert inter.calls[0][0] == "delete"


async def test_send_failure_is_swallowed():
    inter = FakeInteraction(done=False)

    async def boom(*a, **k):
        raise discord.HTTPException(SimpleNamespace(status=404, reason="x"), "gone")

    inter.response.send_message = boom
    await send_error(inter, "❌ nope")  # must not raise
