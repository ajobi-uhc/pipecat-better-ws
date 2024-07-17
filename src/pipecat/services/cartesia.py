#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import json
import uuid
import base64
import asyncio
import time

from typing import AsyncGenerator

from pipecat.processors.frame_processor import FrameDirection
from pipecat.frames.frames import (
    Frame,
    AudioRawFrame,
    StartInterruptionFrame,
    StartFrame,
    EndFrame,
    TextFrame,
    LLMFullResponseEndFrame
)
from pipecat.services.ai_services import TTSService

from loguru import logger

# See .env.example for Cartesia configuration needed
try:
    import websockets
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error(
        "In order to use Cartesia, you need to `pip install pipecat-ai[cartesia]`. Also, set `CARTESIA_API_KEY` environment variable.")
    raise Exception(f"Missing module: {e}")


class CartesiaTTSService(TTSService):

    def __init__(
            self,
            *,
            api_key: str,
            cartesia_version: str = "2024-06-10",
            url: str = "wss://api.cartesia.ai/tts/websocket",
            voice_id: str,
            model_id: str = "sonic-english",
            encoding: str = "pcm_s16le",
            sample_rate: int = 16000,
            language: str = "en",
            **kwargs):
        super().__init__(**kwargs)

        # Aggregating sentences still gives cleaner-sounding results and fewer
        # artifacts than streaming one word at a time. On average, waiting for
        # a full sentence should only "cost" us 15ms or so with GPT-4o or a Llama 3
        # model, and it's worth it for the better audio quality.
        self._aggregate_sentences = True

        # we don't want to automatically push LLM response text frames, because the
        # context aggregators will add them to the LLM context even if we're
        # interrupted. cartesia gives us word-by-word timestamps. we can use those
        # to generate text frames ourselves aligned with the playout timing of the audio!
        self._push_text_frames = False

        self._api_key = api_key
        self._cartesia_version = cartesia_version
        self._url = url
        self._voice_id = voice_id
        self._model_id = model_id
        self._output_format = {
            "container": "raw",
            "encoding": encoding,
            "sample_rate": sample_rate,
        }
        self._language = language

        self._websocket = None
        self._context_id = None
        self._context_id_start_timestamp = None
        self._timestamped_words_buffer = []
        self._receive_task = None
        self._context_appending_task = None
        self._waiting_for_ttfb = False

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self.disconnect()
        pass

    async def connect(self):
        try:
            self._websocket = await websockets.connect(
                f"{self._url}?api_key={self._api_key}&cartesia_version={self._cartesia_version}"
            )
            self._receive_task = self.get_event_loop().create_task(self._receive_task_handler())
            self._context_appending_task = self.get_event_loop().create_task(self._context_appending_task_handler())
        except Exception as e:
            logger.exception(f"{self} initialization error: {e}")
            self._websocket = None

    async def disconnect(self):
        try:
            if self._context_appending_task:
                self._context_appending_task.cancel()
                self._context_appending_task = None
            if self._receive_task:
                self._receive_task.cancel()
                self._receive_task = None
            if self._websocket:
                ws = self._websocket
                self._websocket = None
                await ws.close()
            self._context_id = None
            self._context_id_start_timestamp = None
            self._timestamped_words_buffer = []
            self._waiting_for_ttfb = False
            await self.stop_all_metrics()
        except Exception as e:
            logger.exception(f"{self} error closing websocket: {e}")

    async def handle_interruption(self, frame: StartInterruptionFrame, direction: FrameDirection):
        await super().handle_interruption(frame, direction)
        self._context_id = None
        self._context_id_start_timestamp = None
        self._timestamped_words_buffer = []
        await self.stop_all_metrics()
        await self.push_frame(LLMFullResponseEndFrame())

    async def _receive_task_handler(self):
        try:
            async for message in self._websocket:
                msg = json.loads(message)
                # logger.debug(f"Received message: {msg['type']} {msg['context_id']}")
                if not msg or msg["context_id"] != self._context_id:
                    continue
                if msg["type"] == "done":
                    # unset _context_id but not the _context_id_start_timestamp because we are likely still
                    # playing out audio and need the timestamp to set send context frames
                    self._context_id = None
                    self._timestamped_words_buffer.append(["LLMFullResponseEndFrame", 0])
                if msg["type"] == "timestamps":
                    # logger.debug(f"TIMESTAMPS: {msg}")
                    self._timestamped_words_buffer.extend(
                        list(zip(msg["word_timestamps"]["words"], msg["word_timestamps"]["end"]))
                    )
                    continue
                if msg["type"] == "chunk":
                    if not self._context_id_start_timestamp:
                        self._context_id_start_timestamp = time.time()
                    if self._waiting_for_ttfb:
                        await self.stop_ttfb_metrics()
                        self._waiting_for_ttfb = False
                    frame = AudioRawFrame(
                        audio=base64.b64decode(msg["data"]),
                        sample_rate=self._output_format["sample_rate"],
                        num_channels=1
                    )
                    await self.push_frame(frame)
        except Exception as e:
            logger.exception(f"{self} exception: {e}")

    async def _context_appending_task_handler(self):
        try:
            while True:
                await asyncio.sleep(0.1)
                if not self._context_id_start_timestamp:
                    continue
                elapsed_seconds = time.time() - self._context_id_start_timestamp
                # pop all words from self._timestamped_words_buffer that are older than the
                # elapsed time and print a message about them to the console
                while self._timestamped_words_buffer and self._timestamped_words_buffer[0][1] <= elapsed_seconds:
                    word, timestamp = self._timestamped_words_buffer.pop(0)
                    if word == "LLMFullResponseEndFrame" and timestamp == 0:
                        await self.push_frame(LLMFullResponseEndFrame())
                        continue
                    # print(f"Word '{word}' with timestamp {timestamp:.2f}s has been spoken.")
                    await self.push_frame(TextFrame(word))
        except Exception as e:
            logger.exception(f"{self} exception: {e}")

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"Generating TTS: [{text}]")

        try:
            if not self._websocket:
                await self.connect()

            if not self._waiting_for_ttfb:
                await self.start_ttfb_metrics()
                self._waiting_for_ttfb = True

            if not self._context_id:
                self._context_id = str(uuid.uuid4())

            msg = {
                "transcript": text + " ",
                "continue": True,
                "context_id": self._context_id,
                "model_id": self._model_id,
                "voice": {
                    "mode": "id",
                    "id": self._voice_id
                },
                "output_format": self._output_format,
                "language": self._language,
                "add_timestamps": True,
            }
            # logger.debug(f"SENDING MESSAGE {json.dumps(msg)}")
            try:
                await self._websocket.send(json.dumps(msg))
            except Exception as e:
                logger.exception(f"{self} error sending message: {e}")
                await self.disconnect()
                await self.connect()
                return
            yield None
        except Exception as e:
            logger.exception(f"{self} exception: {e}")
