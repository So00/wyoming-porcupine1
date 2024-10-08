#!/usr/bin/env python3
import argparse
import asyncio
import logging
import platform
import struct
import time
from pprint import pformat
from collections import defaultdict
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional

import pvporcupine
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, WakeModel, WakeProgram
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.wake import Detect, Detection, NotDetected

from . import __version__

_LOGGER = logging.getLogger()
_DIR = Path(__file__).parent

DEFAULT_KEYWORD = "porcupine"


@dataclass
class Keyword:
    """Single porcupine keyword"""

    language: str
    name: str
    model_path: Path


@dataclass
class Detector:
    porcupine: pvporcupine.Porcupine
    sensitivity: float


class State:
    """State of system"""

    def __init__(self, pv_lib_paths: Dict[str, Path], keywords: Dict[str, Keyword]):
        self.pv_lib_paths = pv_lib_paths
        self.keywords = keywords

        # keyword name -> [detector]
        self.detector_cache: Dict[str, List[Detector]] = defaultdict(list)
        self.detector_lock = asyncio.Lock()

    # We set only one keyword_name here, could be multiple
    async def get_porcupine(self, sensitivity: float) -> Detector:
        if self.keywords is None:
            _LOGGER.debug("No keywords")
            raise ValueError(f"No keywords")

        # Check cache first for matching detector
        # async with self.detector_lock:
        #     detectors = self.detector_cache.get(keyword_name)
        #     if detectors:
        #         detector = next(
        #             (d for d in detectors if d.sensitivity == sensitivity), None
        #         )
        #         if detector is not None:
        #             # Remove from cache for use
        #             detectors.remove(detector)

        #             _LOGGER.debug(
        #                 "Using detector for %s from cache (%s)",
        #                 keyword_name,
        #                 len(detectors),
        #             )
        #             return detector

        # _LOGGER.debug("Loading %s for %s", keyword.name, keyword.language)
        # We just set one keyword in keyword_paths, could be multiple
        keywords_paths = []

        # /!\ On ne peut charger QUE des path dont le language est le même que celui de la lib utilisé, sinon on a une ValueError
        for keyword in self.keywords:
            if (self.keywords[keyword].language == "en"):
                keywords_paths.append(str(self.keywords[keyword].model_path))
        _LOGGER.debug("pv_lib_path: %s \n keywords_path %s", pformat(self.pv_lib_paths), pformat(str(keywords_paths[0])))
        porcupine = pvporcupine.create(
            model_path=str(self.pv_lib_paths["en"]),
            # keyword_paths=[str(self.keywords['alexa'].model_path), str(self.keywords['computer'].model_path)],
            keyword_paths=keywords_paths,
            # sensitivities=[sensitivity] * len(keywords_paths),
        )

        return Detector(porcupine, sensitivity)


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser()
    # since default is stio, server launched is https://github.com/rhasspy/wyoming/blob/e61742fc40690a7a66c3c1fbcf5fee665a633189/wyoming/server.py#L85
    parser.add_argument("--uri", default="stdio://", help="unix:// or tcp://")
    parser.add_argument(
        "--data-dir", default=_DIR / "data", help="Path to directory lib/resources"
    )
    parser.add_argument("--system", help="linux or raspberry-pi")
    parser.add_argument("--sensitivity", type=float, default=0.5)
    #
    parser.add_argument("--debug", action="store_true", help="Log DEBUG messages")
    parser.add_argument(
        "--log-format", default=logging.BASIC_FORMAT, help="Format for log messages"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
        help="Print version and exit",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO, format=args.log_format
    )
    _LOGGER.debug(args)

    if not args.system:
        machine = platform.machine().lower()
        if ("arm" in machine) or ("aarch" in machine):
            args.system = "raspberry-pi"
        else:
            args.system = "linux"

    args.data_dir = Path(args.data_dir)

    # lang -> path
    pv_lib_paths: Dict[str, Path] = {}
    for lib_path in (args.data_dir / "lib" / "common").glob("*.pv"):
        lib_lang = lib_path.stem.split("_")[-1]
        pv_lib_paths[lib_lang] = lib_path

    # name -> keyword
    keywords: Dict[str, Keyword] = {}
    _LOGGER.debug('Files pattern ppn')
    _LOGGER.debug((args.data_dir / "resources").rglob("*.ppn"))
    for kw_path in (args.data_dir / "resources").rglob("*.ppn"):
        kw_system = kw_path.stem.split("_")[-1]
        if kw_system != args.system:
            continue

        kw_lang = kw_path.parent.parent.name
        kw_name = kw_path.stem.rsplit("_", maxsplit=1)[0]
        keywords[kw_name] = Keyword(language=kw_lang, name=kw_name, model_path=kw_path)

    _LOGGER.debug('List of keywords')
    _LOGGER.debug(keywords)
    wyoming_info = Info(
        wake=[
            WakeProgram(
                name="porcupine1",
                description="On-device wake word detection powered by deep learning",
                attribution=Attribution(
                    name="Picovoice", url="https://github.com/Picovoice/porcupine"
                ),
                installed=True,
                version=__version__,
                models=[
                    WakeModel(
                        name=kw.name,
                        description=f"{kw.name} ({kw.language})",
                        phrase=kw.name,
                        attribution=Attribution(
                            name="Picovoice",
                            url="https://github.com/Picovoice/porcupine",
                        ),
                        installed=True,
                        languages=[kw.language],
                        version="1.9.0",
                    )
                    for kw in keywords.values()
                ],
            )
        ],
    )

    # PV lib paths is all the path of the files ending with .pv
    # Keywords is all the keywords loaded with .ppn files
    state = State(pv_lib_paths=pv_lib_paths, keywords=keywords)

    _LOGGER.info("Ready")

    # Start server
    server = AsyncServer.from_uri(args.uri)

    try:
        # The run function is this one https://github.com/rhasspy/wyoming/blob/e61742fc40690a7a66c3c1fbcf5fee665a633189/wyoming/server.py#L31
        await server.run(partial(Porcupine1EventHandler, wyoming_info, args, state))
    except KeyboardInterrupt:
        pass


# -----------------------------------------------------------------------------

class Porcupine1EventHandler(AsyncEventHandler):
    """Event handler for clients."""

    def __init__(
        self,
        wyoming_info: Info,
        cli_args: argparse.Namespace,
        state: State,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.cli_args = cli_args
        self.wyoming_info_event = wyoming_info.event()
        self.client_id = str(time.monotonic_ns())
        self.state = state
        self.converter = AudioChunkConverter(rate=16000, width=2, channels=1)
        self.audio_buffer = bytes()
        self.detected = False

        self.detector: Optional[Detector] = None
        self.chunk_format: str = ""
        self.bytes_per_chunk: int = 0

        _LOGGER.debug("Client connected: %s", self.client_id)

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            _LOGGER.debug("Sent info to client: %s", self.client_id)
            return True

        # I guess we check the event here if it's detected :thinking:
        if Detect.is_type(event.type):
            _LOGGER.debug("Event is detect type")
            detect = Detect.from_event(event)
            if detect.names:
                _LOGGER.debug("We load name %s", detect.names[0])
                # TODO: use all names
                await self._load_keyword()
        elif AudioStart.is_type(event.type):
            _LOGGER.debug("Audio just start. Detected pass to false")
            self.detected = False
        elif AudioChunk.is_type(event.type):
            _LOGGER.debug("Audio chuck type")
            if self.detector is None:
                _LOGGER.debug("Self detector was none, load keyword")
                # Default keyword
                await self._load_keyword()

            assert self.detector is not None

            chunk = AudioChunk.from_event(event)
            chunk = self.converter.convert(chunk)
            self.audio_buffer += chunk.audio

            while len(self.audio_buffer) >= self.bytes_per_chunk:
                unpacked_chunk = struct.unpack_from(
                    self.chunk_format, self.audio_buffer[: self.bytes_per_chunk]
                )
                # for detector in self.detectors:
                #   keyword_index = detector.porcupine.process(unpacked_chunk)
                #   _LOGGER.debug("Keyword index in loop %d", keyword_index)
                #   if keyword_index >= 0:
                #       _LOGGER.debug("Detected keyword %s", detector.keyword)
                #       # You can add additional logic here to handle the detected keyword
                #       await self.write_event(
                #         Detection(
                #             name=detector.keyword, timestamp=chunk.timestamp
                #         ).event()
                #       )

                # Here we get the result of the actual detected keywords
                # That could look something like that actually https://github.com/Picovoice/porcupine/blob/1462f5c8c7a8985fca50eec350deaef973407e67/demo/python/porcupine_demo_file.py#L138
                keyword_index = self.detector.porcupine.process(unpacked_chunk)
                _LOGGER.debug("Keyword index in loop %d", keyword_index)
                if keyword_index >= 0:
                    # TODO: add logic here to handle the detected keyword
                    # _LOGGER.debug("Detected %s from client %s", self.state.keywords[keyword_index].name, self.client_id)
                    # Here we may need to write an event like here for detection of assistant needed
                    # Or an event on mqtt for wake words who will do an action
                    # If we can do something in config for that, awesome, but first, let's get it work
                    await self.write_event(
                        Detection(
                          # TODO: remove hard coded value
                            name='alexa', timestamp=chunk.timestamp
                        ).event()
                    )

                self.audio_buffer = self.audio_buffer[self.bytes_per_chunk :]

        elif AudioStop.is_type(event.type):
            _LOGGER.debug("Audio stop")
            # Inform client if not detections occurred
            if not self.detected:
                _LOGGER.debug("Nothing was detected")
                # No wake word detections
                await self.write_event(NotDetected().event())

                _LOGGER.debug("Audio stopped without detection from client: %s", self.client_id)

            return False
        else:
            _LOGGER.debug("Unexpected event: type=%s, data=%s", event.type, event.data)

        return True

    async def disconnect(self) -> None:
        _LOGGER.debug("Client disconnected: %s", self.client_id)

        if self.detector is not None:
            # Return detector to cache
            async with self.state.detector_lock:
                # self.state.detector_cache[self.keyword_name].append(self.detector)
                self.detector = None
                # _LOGGER.debug(
                #     "Detector for %s returned to cache (%s)",
                #     self.keyword_name,
                #     len(self.state.detector_cache[self.keyword_name]),
                # )

    async def _load_keyword(self):
        # Here we set self.detector, this could be self.detectors
        self.detector = await self.state.get_porcupine(
            self.cli_args.sensitivity
        )
        self.chunk_format = "h" * self.detector.porcupine.frame_length
        self.bytes_per_chunk = self.detector.porcupine.frame_length * 2

# -----------------------------------------------------------------------------


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
