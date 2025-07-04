# telegram_input.py
"""
Telegram I/O wrapper for the Assembler.

Features
────────
• Feeds user text into `asm.run_with_meta_context`
• Live “🛠️ Processing…” message that updates once per stage
• Replaces the placeholder with the full answer (chunked if >4 000 chars)
• Streams exactly ONE .ogg voice reply
• Accepts **voice notes** – they’re run through Whisper, then processed as text
• Maintains a SQLite-backed user registry for DM’ing users by username
• **Pins** each bot reply for cross-bot notification, then **un-pins** it
• Listens for **pin** events to ingest other bots’ outputs as context
"""

from __future__ import annotations
import asyncio
import os
import queue as _queue
import tempfile
import subprocess
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from context import HybridContextRepository, ContextObject, _locked, sanitize_jsonl
from assembler import Assembler
from tts_service import TTSManager
from user_registry import _REG
from group_registry import _GREG

import whisper
_WHISPER = whisper.load_model("base")  # load once

# basic logging
logging.basicConfig(
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# chat_id → dedicated Assembler instance
assemblers: dict[int, Assembler] = {}

# chat_id → pending inference queue
_pending: dict[int, asyncio.Queue[Tuple[str,int]]] = {}

# ────────────────────────────────────────────────────────────────────────
# Helper to split & send long text (async), with optional reply_to
# ────────────────────────────────────────────────────────────────────────
async def _send_long_text_async(
    bot,
    chat_id: int,
    text: str,
    *,
    chunk_size: int = 3800,
    reply_to: Optional[int] = None
) -> None:
    if not text.strip():
        return
    import re
    paras = text.split("\n\n")
    buffer, chunks = "", []
    for para in paras:
        if len(buffer) + len(para) + 2 <= chunk_size:
            buffer = (buffer + "\n\n" + para).strip()
        else:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            if len(para) <= chunk_size:
                buffer = para
            else:
                for sent in re.split(r"(?<=[\.\?\!])\s+", para):
                    if len(buffer) + len(sent) + 1 <= chunk_size:
                        buffer = (buffer + " " + sent).strip()
                    else:
                        if buffer:
                            chunks.append(buffer)
                        buffer = ""
                        for i in range(0, len(sent), chunk_size):
                            chunks.append(sent[i : i + chunk_size])
    if buffer:
        chunks.append(buffer)

    for part in chunks:
        # send the chunk
        msg = await bot.send_message(
            chat_id=chat_id,
            text=part,
            reply_to_message_id=reply_to
        )
        # auto-pin & unpin so other bots see it
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id)
            await bot.unpin_chat_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception as e:
            logger.warning(f"pin/unpin failed for msg {msg.message_id}: {e}")


def make_per_chat_repo(chat_id: int, archive_max_mb: float = 10.0) -> HybridContextRepository:
    """
    Ensure the JSONL is clean, then return a HybridContextRepository
    that shards context_<chat_id>.jsonl + context_<chat_id>.db.
    """
    jsonl_path  = f"context_{chat_id}.jsonl"
    sqlite_path = f"context_{chat_id}.db"
    sanitize_jsonl(jsonl_path)
    return HybridContextRepository(
        jsonl_path=jsonl_path,
        sqlite_path=sqlite_path,
        archive_max_mb=archive_max_mb,
    )


# ────────────────────────────────────────────────────────────────────────
# Factory for per-stage status callback
# ────────────────────────────────────────────────────────────────────────
def _make_status_cb(
    loop: asyncio.AbstractEventLoop,
    bot,
    chat_id: int,
    msg_id: int,
    *,
    max_lines: int = 10,
    min_interval: float = 5,
):
    from typing import List, Any
    history: List[str] = []
    last_edit_at: float = 0.0
    pending_handle: asyncio.TimerHandle | None = None
    disabled = False

    async def _do_edit():
        nonlocal last_edit_at, pending_handle
        if disabled:
            return
        # Show the JSONL file being updated, then the recent stage updates
        header = f"`context_{chat_id}.jsonl` updating...\n"
        body = "\n".join(history[-max_lines:])
        text = header + body
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode='Markdown'
            )
        except:
            pass
        last_edit_at = loop.time()
        pending_handle = None

    def _schedule_edit():
        nonlocal pending_handle
        if disabled or pending_handle:
            return
        delay = max(min_interval - (loop.time() - last_edit_at), 0.0)
        if delay <= 0:
            asyncio.run_coroutine_threadsafe(_do_edit(), loop)
        else:
            pending_handle = loop.call_later(
                delay,
                lambda: asyncio.run_coroutine_threadsafe(_do_edit(), loop)
            )

    def status_cb(stage: str, output: Any):
        nonlocal history
        if disabled:
            return
        snippet = str(output).replace("\n", " ")
        if len(snippet) > 1000:
            snippet = snippet[:997] + "…"
        # Bold the stage name
        history.append(f"• *{stage}*: {snippet}")
        _schedule_edit()

    def stop_cb():
        nonlocal disabled, pending_handle
        disabled = True
        if pending_handle and not pending_handle.cancelled():
            pending_handle.cancel()
            pending_handle = None

    return status_cb, stop_cb



# ────────────────────────────────────────────────────────────────────────
# Handler for ANY pinned message in any chat
# ────────────────────────────────────────────────────────────────────────
async def _on_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.pinned_message:
        return

    chat_id = update.effective_chat.id
    pinned = msg.pinned_message

    pinner = (msg.from_user.username or str(msg.from_user.id)).lower()
    author = (pinned.from_user.username or str(pinned.from_user.id)).lower()
    text   = pinned.text or pinned.caption or "<non-text content>"

    # Only ingest pins created by bots
    if not msg.from_user.is_bot:
        return

    seg = ContextObject.make_segment(
        semantic_label="bot_pinned",
        content_refs=[],
        tags=["bot_message", "pinned", f"pinner:{pinner}", f"author:{author}"],
        metadata={"pinner":pinner, "author":author, "text":text}
    )
    seg.summary  = text
    seg.stage_id = "telegram_pinned"
    seg.touch()

    asm = assemblers.get(chat_id)
    if asm:
        asm.repo.save(seg)


# ────────────────────────────────────────────────────────────────────────
# Main entry-point: launch the Telegram bot
# ────────────────────────────────────────────────────────────────────────
def telegram_input(asm: Assembler):
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing BOT_TOKEN env var")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    req = HTTPXRequest(connect_timeout=20, read_timeout=20)
    app = ApplicationBuilder().token(token).request(req).build()

    running: dict[tuple[int,int], asyncio.Task] = {}
    _pending: dict[tuple[int,int], asyncio.Queue[Tuple[str,int]]] = {}
    asm._chat_contexts = set()

    # register the pin‐event listener
    app.add_handler(
        MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, _on_pin),
        group=0
    )

    async def _handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id   = update.effective_chat.id
        chat_type = update.effective_chat.type
        bot_name  = context.bot.username.lower()

        # 1) Catch every kind of incoming update
        msg = (
            update.message
            or update.edited_message
            or update.channel_post
            or update.edited_channel_post
        )
        if not msg:
            return

        # 2) Determine kind & raw data for logging & memory
        kind, data = "other", None
        if msg.text:
            kind, data = "text", msg.text
        elif msg.photo:
            kind, data = "photo", msg.photo[-1].file_id
        elif msg.document:
            kind, data = "document", msg.document.file_id
        elif msg.sticker:
            kind, data = "sticker", msg.sticker.file_unique_id
        elif msg.voice:
            kind, data = "voice", msg.voice.file_id
        elif msg.video:
            kind, data = "video", msg.video.file_id
        elif msg.location:
            loc = msg.location
            kind, data = "location", f"{loc.latitude},{loc.longitude}"
        elif msg.poll:
            kind, data = "poll", msg.poll.question
        else:
            kind, data = "other", repr(msg.to_dict())

        now = datetime.utcnow().isoformat() + "Z"
        logger.info(f"[{now}] Incoming update — chat_id={chat_id} kind={kind} data={data!r}")

        # 3) Record user & group in registries
        user = update.effective_user
        if user and user.username:
            _REG.add(user.username, user.id)
        if chat_type in ("group", "supergroup", "channel"):
            title = update.effective_chat.title or f"chat_{chat_id}"
            _GREG.add(title, chat_id)

        # 4) Instantiate per-chat Assembler if needed
        chat_asm = assemblers.get(chat_id)
        if chat_asm is None:
            repo = make_per_chat_repo(chat_id)
            tts = TTSManager(logger=asm.tts.log, cfg=asm.cfg, audio_service=None)
            tts.set_mode("file")
            chat_asm = Assembler(
                context_path     = f"context_{chat_id}.jsonl",
                config_path      = "config.json",
                lookback_minutes = 60,
                top_k            = 5,
                tts_manager      = tts,
                repo             = repo,
            )
            assemblers[chat_id] = chat_asm
        chat_asm._chat_contexts.add(chat_id)

        # 5) Record *every* update into memory
        tags = ["telegram_update", kind]
        tags.append("group" if chat_type in ("group","supergroup") else "private")
        # 5) Record *every* update into memory
        seg = ContextObject.make_segment(
            semantic_label=f"tg_{kind}",
            content_refs=[],
            tags=tags,
            metadata={
                "data": data,
                "telegram_message_id": msg.message_id
            }
        )
        seg.summary  = str(data)
        seg.stage_id = "telegram_update"
        seg.touch()

        # if this Telegram message was a reply, link back to the original ContextObject
        if msg.reply_to_message:
            orig = next(
                (c for c in chat_asm.repo.query(
                    lambda c: c.metadata.get("telegram_message_id") 
                              == msg.reply_to_message.message_id
                )),
                None
            )
            if orig:
                seg.references.append(orig.context_id)

        chat_asm.repo.save(seg)

        # 6) Extract or transcribe text if any
        text = (msg.text or "").strip()
        if msg.voice and not text:
            try:
                raw_ogg = tempfile.mktemp(suffix=".oga")
                vf = await context.bot.get_file(msg.voice.file_id)
                await vf.download_to_drive(raw_ogg)
                wav = raw_ogg + ".wav"
                subprocess.run(
                    ["ffmpeg","-y","-loglevel","error",
                     "-i",raw_ogg,"-ac","1","-ar","16000",wav],
                    check=True
                )
                result = _WHISPER.transcribe(wav, language="en")
                text = result.get("text","").strip() or text
            except Exception as ex:
                await context.bot.send_message(
                    chat_id,
                    f"❌ Voice note error: {ex}",
                    reply_to_message_id=msg.message_id
                )
            finally:
                for p in (locals().get("raw_ogg"), locals().get("wav")):
                    if p and os.path.exists(p):
                        try: os.unlink(p)
                        except: pass

        # 7) Handle private slash-commands
        if chat_type == "private" and text.startswith("/"):
            parts = text.split(None, 2)
            cmd = parts[0].lower()
            if cmd == "/list_users":
                users = _REG.list_all()
                await context.bot.send_message(
                    chat_id,
                    "\n".join(f"@{u['username']}: {u['id']}" for u in users) or "(none)"
                )
                return
            if cmd == "/list_groups":
                groups = _GREG.list_all()
                await context.bot.send_message(
                    chat_id,
                    "\n".join(f"{g['name']}: {g['chat_id']}" for g in groups) or "(none)"
                )
                return
            if cmd == "/dm" and len(parts) == 3:
                target, body = parts[1], parts[2]
                target_id = _REG.id_for(target.lstrip("@").lower())
                if not target_id:
                    await context.bot.send_message(
                        chat_id, f"User @{target} not found.",
                        reply_to_message_id=msg.message_id
                    )
                else:
                    await context.bot.send_message(target_id, f"DM from @{user.username}: {body}")
                    await context.bot.send_message(
                        chat_id, f"✔️ Sent to @{target}",
                        reply_to_message_id=msg.message_id
                    )
                return
            if cmd == "/gm" and len(parts) == 3:
                grp, body = parts[1], parts[2]
                grp_id = _GREG.id_for(grp)
                if not grp_id:
                    await context.bot.send_message(
                        chat_id, f"Group '{grp}' not found.",
                        reply_to_message_id=msg.message_id
                    )
                else:
                    await context.bot.send_message(grp_id, f"[Group DM] {body}")
                    await context.bot.send_message(
                        chat_id, f"✔️ Sent to group '{grp}'",
                        reply_to_message_id=msg.message_id
                    )
                return

        # 8) Decide whether to run inference
        wants_reply = False
        if text:
            try:
                wants_reply = await asyncio.to_thread(chat_asm.filter_callback, text)
            except Exception:
                wants_reply = False

        do_infer = (
            chat_type == "private"
            or msg.voice
            or wants_reply
            or any(
                ent.type == "mention"
                and msg.text[ent.offset:ent.offset+ent.length]
                       .lstrip("@").lower() == bot_name
                for ent in (msg.entities or [])
            )
            or (msg.reply_to_message and msg.reply_to_message.from_user.id == context.bot.id)
        )
        if not do_infer:
            return
        # 9) Build prompt & enqueue (per-user in group)
        sender    = user.username or user.first_name
        user_text = f"{sender}: {text}"
        trigger_id = msg.message_id

        # use a (chat_id, user_id) tuple so each user has its own queue
        user_id = user.id
        key     = (chat_id, user_id)

        # grab or create that user’s queue
        queue = _pending.setdefault(key, asyncio.Queue())

        # if they’re already running an inference, enqueue
        if (prev := running.get(key)) and not prev.done():
            await context.bot.send_message(
                chat_id,
                "⚠️ I’m still working on your previous request—I’ll handle this one next.",
                reply_to_message_id=trigger_id
            )
            await queue.put((user_text, trigger_id))
            return

        # ────────────────────────────────────────────────────────────────
        # helper to kick off a runner, pinning/unpinning replies
        # ────────────────────────────────────────────────────────────────
        async def start_runner(request_text: str, reply_to_id: int):
            placeholder = await context.bot.send_message(
                chat_id=chat_id,
                text=f"init reply to chat id {reply_to_id}...",
                reply_to_message_id=reply_to_id
            )
            placeholder_id = placeholder.message_id
            status_cb, stop_status = _make_status_cb(
                loop, context.bot, chat_id, placeholder_id
            )

            async def runner():
                try:
                    # clear old TTS queues
                    for qobj in (chat_asm.tts._file_q, chat_asm.tts._ogg_q):
                        while True:
                            try:
                                qobj.get_nowait()
                            except _queue.Empty:
                                break

                    chat_asm.tts.set_mode("file")
                    final = await asyncio.to_thread(
                        chat_asm.run_with_meta_context,
                        request_text,
                        status_cb
                    )
                    stop_status()

                    # If there's absolutely no response, just delete the placeholder and bail
                    if not final or not final.strip():
                        try:
                            await context.bot.delete_message(
                                chat_id=chat_id,
                                message_id=placeholder_id
                            )
                        except:
                            pass
                        return

                    # Small reply: edit placeholder, then pin & unpin
                    if len(final) < 4000:
                        sent = await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=placeholder_id,
                            text=final
                        )
                        await context.bot.pin_chat_message(
                            chat_id=chat_id,
                            message_id=sent.message_id,
                            disable_notification=True
                        )
                        await asyncio.sleep(1)
                        await context.bot.unpin_chat_message(
                            chat_id=chat_id,
                            message_id=sent.message_id
                        )

                    # Large reply: drop placeholder, stream chunks (each chunk pins/unpins)
                    else:
                        try:
                            await context.bot.delete_message(
                                chat_id=chat_id,
                                message_id=placeholder_id
                            )
                        except:
                            pass
                        await _send_long_text_async(
                            context.bot,
                            chat_id,
                            final,
                            reply_to=reply_to_id
                        )

                    # Now the voice reply as before...
                    chat_asm.tts.enqueue(final)
                    await asyncio.to_thread(chat_asm.tts._file_q.join)
                    oggs: List[str] = []
                    while True:
                        try:
                            p = chat_asm.tts._ogg_q.get_nowait()
                            if os.path.getsize(p) > 0:
                                oggs.append(p)
                        except _queue.Empty:
                            break

                    if oggs:
                        if len(oggs) == 1:
                            with open(oggs[0], "rb") as vf:
                                await context.bot.send_voice(
                                    chat_id=chat_id,
                                    voice=vf,
                                    reply_to_message_id=reply_to_id
                                )
                        else:
                            combined = os.path.join(
                                tempfile.gettempdir(),
                                f"combined_{uuid.uuid4().hex}.ogg"
                            )
                            ins     = sum([["-i", p] for p in oggs], [])
                            streams = "".join(f"[{i}:a]" for i in range(len(oggs)))
                            filt    = f"{streams}concat=n={len(oggs)}:v=0:a=1,aresample=48000"
                            subprocess.run(
                                ["ffmpeg","-y","-loglevel","error", *ins,
                                "-filter_complex", filt,
                                "-c:a","libopus","-b:a","48k", combined],
                                check=True
                            )
                            with open(combined, "rb") as vf:
                                await context.bot.send_voice(
                                    chat_id=chat_id,
                                    voice=vf,
                                    reply_to_message_id=reply_to_id
                                )

                except asyncio.CancelledError:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=placeholder_id,
                            text="⚠️ Previous request cancelled."
                        )
                    except:
                        pass

                except Exception as e:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Error: {e}",
                        reply_to_message_id=reply_to_id
                    )

                finally:
                    running.pop(chat_id, None)
                    try:
                        nxt, nxt_id = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await start_runner(nxt, nxt_id)

            # schedule it exactly as before:
            running[chat_id] = loop.create_task(runner())



        # start it!
        await start_runner(user_text, trigger_id)


    # primary message handler
    app.add_handler(
        MessageHandler((filters.TEXT | filters.VOICE) & ~filters.COMMAND, _handle),
        group=1
    )

    loop.run_until_complete(app.initialize())
    loop.run_until_complete(app.start())
    loop.run_until_complete(app.updater.start_polling())
    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(app.updater.stop_polling())
        loop.run_until_complete(app.stop())
        loop.run_until_complete(app.shutdown())
        loop.close()