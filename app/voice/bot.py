import asyncio
import json
from datetime import datetime

from loguru import logger

from pipecat.transports.daily.transport import DailyTransport, DailyParams
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    Frame,
    TextFrame,
    EndFrame,
    LLMFullResponseEndFrame,
    TranscriptionFrame,
)
from pipecat.transports.daily.transport import DailyOutputTransportMessageFrame
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContext,
    LLMContextAggregatorPair,
    LLMContextFrame,
)
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from app.config import settings
from app.voice.interviewer_llm import build_system_prompt
from app.voice.emotion_tracker import EmotionTracker
from app.voice.emotion_processor import EmotionMonitorProcessor, EmotionContextInjector


class TranscriptCollector:
    """Collects transcript entries during the interview."""

    def __init__(self):
        self.entries: list[dict] = []

    def add_entry(self, role: str, text: str):
        self.entries.append({
            "role": role,
            "text": text,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def get_transcript(self) -> list[dict]:
        return self.entries


class InterviewEndDetector(FrameProcessor):
    """Detects end-of-interview signals in LLM output and ends the pipeline.

    Detects both:
    1. [END_INTERVIEW] marker (may arrive split across multiple streamed TextFrames)
    2. Closing phrase "feedback report shortly" as a backup trigger
    """

    END_MARKER = "[END_INTERVIEW]"
    CLOSING_PHRASES = ("feedback report shortly", "ending the session now")

    def __init__(self, task: PipelineTask | None = None, **kwargs):
        super().__init__(**kwargs)
        self._task: PipelineTask | None = task
        self._should_end = False
        self._text_buffer = ""  # Accumulates text to detect split markers

    def set_task(self, task: PipelineTask):
        self._task = task

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TextFrame):
            self._text_buffer += frame.text

            # Check for closing phrases (normal end + early exit)
            if not self._should_end and any(phrase in self._text_buffer.lower() for phrase in self.CLOSING_PHRASES):
                self._should_end = True
                logger.info("Detected closing phrase — will end after TTS finishes")

            # Check for [END_INTERVIEW] marker (may be split across frames)
            if self.END_MARKER in self._text_buffer:
                self._should_end = True
                logger.info("Detected END_INTERVIEW marker — will end after TTS finishes")
                # Strip the marker so TTS doesn't speak it
                frame.text = frame.text.replace(self.END_MARKER, "").replace("[END_INTERVIEW", "").replace("END_INTERVIEW]", "").strip()
                if frame.text:
                    await self.push_frame(frame, direction)
                return

            # Suppress any partial marker tokens from reaching TTS
            # e.g. "[END", "_INTERVIEW", "]"
            if "[" in self._text_buffer and self._text_buffer.rstrip().endswith(("[", "[END", "[END_", "[END_I", "[END_IN", "[END_INT", "[END_INTE", "[END_INTER", "[END_INTERV", "[END_INTERVI", "[END_INTERVIE", "[END_INTERVIEW")):
                # Hold back — don't forward partial marker to TTS
                return

        if isinstance(frame, LLMFullResponseEndFrame) and self._should_end:
            await self.push_frame(frame, direction)
            logger.info("Scheduling pipeline end after final TTS playback")
            if self._task:
                await asyncio.sleep(5)
                await self._task.queue_frames([EndFrame()])
            self._text_buffer = ""
            return

        await self.push_frame(frame, direction)


class UserTranscriptForwarder(FrameProcessor):
    """Captures final STT transcriptions and sends them to the frontend as app messages."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            user_text = frame.text.strip()
            if user_text:
                msg = DailyOutputTransportMessageFrame(
                    message={"type": "transcript", "role": "user", "text": user_text}
                )
                await self.push_frame(msg, FrameDirection.DOWNSTREAM)

        await self.push_frame(frame, direction)


class InterviewMetadataTracker(FrameProcessor):
    """Tracks interview progress and sends real-time metadata to the frontend.

    Sits after the LLM in the pipeline. Monitors LLM text output to count
    questions asked, determine current section, and send assistant transcript entries.
    """

    def __init__(self, interview_plan: dict, **kwargs):
        super().__init__(**kwargs)
        self._response_count = 0
        self._current_text = ""

        # Parse plan structure
        categories = interview_plan.get("categories", [])
        self._categories = categories
        self._total_questions = sum(len(c.get("questions", [])) for c in categories)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Accumulate assistant LLM text (but not TranscriptionFrames which are user STT)
        if isinstance(frame, TextFrame) and not isinstance(frame, TranscriptionFrame):
            self._current_text += frame.text

        # On full LLM response end: send metadata + transcript
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._response_count += 1

            # First response is greeting, questions start from 2
            question_number = max(0, self._response_count - 1)

            # Map question number to current section
            current_section_idx = 0
            q_counter = 0
            sections_progress = []
            for i, cat in enumerate(self._categories):
                cat_q_count = len(cat.get("questions", []))
                completed = min(cat_q_count, max(0, question_number - q_counter))
                sections_progress.append({
                    "name": cat["name"],
                    "completed": completed,
                    "total": cat_q_count,
                })
                if question_number > q_counter and question_number <= q_counter + cat_q_count:
                    current_section_idx = i
                elif question_number > q_counter + cat_q_count:
                    current_section_idx = min(i + 1, len(self._categories) - 1)
                q_counter += cat_q_count

            current_section = self._categories[current_section_idx]["name"] if self._categories else ""

            # Send metadata
            metadata_msg = DailyOutputTransportMessageFrame(
                message={
                    "type": "interview_metadata",
                    "question_number": min(question_number, self._total_questions),
                    "total_questions": self._total_questions,
                    "current_section": current_section,
                    "current_section_index": current_section_idx,
                    "sections": sections_progress,
                    "response_type": "greeting" if self._response_count == 1 else "question",
                }
            )
            await self.push_frame(metadata_msg, FrameDirection.DOWNSTREAM)

            # Send assistant transcript entry
            assistant_text = self._current_text.strip()
            if assistant_text:
                transcript_msg = DailyOutputTransportMessageFrame(
                    message={"type": "transcript", "role": "assistant", "text": assistant_text}
                )
                await self.push_frame(transcript_msg, FrameDirection.DOWNSTREAM)

            self._current_text = ""

        await self.push_frame(frame, direction)


async def run_interview_bot(
    room_url: str,
    bot_token: str,
    interview_plan: dict,
    resume_text: str,
    job_description: str | None = None,
    company_info: dict | None = None,
    role_type: str | None = None,
    level: str | None = None,
    years_of_experience: int | None = None,
    employment_type: str | None = None,
) -> list[dict]:
    """Run the Pipecat interview bot. Returns the transcript when done."""

    transcript = TranscriptCollector()
    system_prompt = build_system_prompt(
        interview_plan=interview_plan,
        resume_text=resume_text,
        job_description=job_description,
        company_info=company_info,
        role_type=role_type,
        level=level,
        years_of_experience=years_of_experience,
        employment_type=employment_type,
    )

    # Configure transport
    transport = DailyTransport(
        room_url=room_url,
        token=bot_token,
        bot_name="AI Interviewer",
        params=DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    # Configure services
    stt = DeepgramSTTService(
        api_key=settings.deepgram_api_key,
    )

    tts = ElevenLabsTTSService(
        api_key=settings.elevenlabs_api_key,
        settings=ElevenLabsTTSService.Settings(
            voice="21m00Tcm4TlvDq8ikWAM",  # "Rachel" - professional female voice
            stability=0.6,
        ),
    )

    llm = AnthropicLLMService(
        api_key=settings.claude_api_key,
        settings=AnthropicLLMService.Settings(
            model="claude-sonnet-4-6",
        ),
    )

    # -- Tool: check_interview_progress --
    check_progress_schema = FunctionSchema(
        name="check_interview_progress",
        description="Check if the candidate has answered enough questions to receive a feedback report. MUST be called whenever the candidate wants to end, stop, or leave the interview early.",
        properties={},
        required=[],
    )

    tools = ToolsSchema(standard_tools=[check_progress_schema])

    async def handle_check_progress(params):
        """Count real questions asked and substantive answers given."""
        entries = transcript.get_transcript()
        assistant_entries = [e for e in entries if e["role"] == "assistant"]
        questions_asked = max(0, len(assistant_entries) - 1)

        substantive_answers = [
            e for e in entries
            if e["role"] == "user"
            and len(e.get("text", "").strip().split()) >= 5
        ]
        questions_answered = len(substantive_answers)

        can_generate_report = questions_answered >= 3
        result = {
            "questions_asked": questions_asked,
            "questions_answered": questions_answered,
            "minimum_required": 3,
            "can_generate_report": can_generate_report,
        }
        logger.info(f"check_interview_progress: {result}")
        await params.result_callback(json.dumps(result))

    # Build context with system prompt + tools
    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt}],
        tools=tools,
    )
    context_aggregator = LLMContextAggregatorPair(context)

    # Register the tool handler with the LLM
    llm.register_function("check_interview_progress", handle_check_progress)

    # End-of-interview detector (inserted between LLM and TTS)
    end_detector = InterviewEndDetector()

    # Real-time metadata — sends progress + transcript to frontend via Daily app messages
    user_transcript_fwd = UserTranscriptForwarder()
    metadata_tracker = InterviewMetadataTracker(interview_plan=interview_plan)

    # Emotion awareness (multi-agent)
    emotion_tracker = EmotionTracker(check_interval=6)
    emotion_injector = EmotionContextInjector(tracker=emotion_tracker, context=context)
    emotion_monitor = EmotionMonitorProcessor(tracker=emotion_tracker, context=context)

    # Build pipeline
    # Flow: input → STT → user_transcript_fwd → user_agg → emotion_injector → LLM → emotion_monitor → metadata_tracker → end_detector → TTS → output → assistant_agg
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_transcript_fwd,    # Sends user STT text to frontend
            context_aggregator.user(),
            emotion_injector,       # Injects emotion notes before LLM sees the context
            llm,
            emotion_monitor,        # Observes responses, triggers Feeling Agent in background
            metadata_tracker,       # Sends interview progress + assistant transcript to frontend
            end_detector,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
    )

    # Give the detector a reference to the task so it can send EndFrame
    end_detector.set_task(task)

    # Event handlers
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client, **kwargs):
        logger.info(f"Client connected: {client}")
        # Trigger the LLM to start the interview
        await task.queue_frames([LLMContextFrame(context=context)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client, **kwargs):
        logger.info(f"Client disconnected: {client}")
        await task.cancel()

    # Run the pipeline
    runner = PipelineRunner()
    await runner.run(task)

    # Extract transcript from context messages
    if context.messages:
        for msg in context.messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                transcript.add_entry(
                    role, content if isinstance(content, str) else str(content)
                )

    return transcript.get_transcript()
