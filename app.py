"""Browser entry point (FR-12), served by Chainlit:

    chainlit run app.py

A thin shell. All behavior lives in `chat_session.py`, which receives the session
store and a send function; this file only connects those to Chainlit.

Per-session state (the ReviewContext and the last Report) lives ONLY in
`cl.user_session`, one per browser session. This module defines functions and
nothing else — no module-level container that two sessions could share.
"""

import chainlit as cl

from chat_session import WELCOME, handle_message, start_session
from ledger import register_ledger


@cl.on_app_startup
def on_app_startup() -> None:
    # FR-11: the ledger's single registration line, identical to main.py's.
    # Runs once per process, not once per browser session.
    register_ledger()


@cl.on_chat_start
async def on_chat_start() -> None:
    start_session(cl.user_session)
    await cl.Message(content=WELCOME).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    async def send(text: str) -> None:
        await cl.Message(content=text).send()

    # Awaited end to end — no synchronous variant exists (spec.md §4.12).
    await handle_message(cl.user_session, message.content, send)
