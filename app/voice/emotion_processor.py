"""Pipecat processors for multi-agent emotion awareness.

EmotionMonitorProcessor: Observes the conversation and triggers the Feeling Agent.
EmotionContextInjector: Injects emotion notes into the LLM context before each call.
"""

from loguru import logger

from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMMessagesAppendFrame,
)
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContext,
    LLMContextFrame,
)

from app.voice.emotion_tracker import EmotionTracker


class EmotionMonitorProcessor(FrameProcessor):
    """Observes conversation flow and triggers emotion analysis.

    Placed AFTER the LLM in the pipeline. When a full LLM response ends,
    it notifies the EmotionTracker with the current conversation context
    so it can analyze the candidate's emotional state in the background.
    """

    def __init__(self, tracker: EmotionTracker, context: LLMContext, **kwargs):
        super().__init__(**kwargs)
        self._tracker = tracker
        self._context = context

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseEndFrame):
            messages = self._context.messages
            if messages:
                user_msgs = [m for m in messages if m.get("role") == "user"]
                if user_msgs:
                    self._tracker.on_candidate_message(list(messages))

        await self.push_frame(frame, direction)


class EmotionContextInjector(FrameProcessor):
    """Injects emotion awareness notes into the LLM context.

    Placed BEFORE the LLM in the pipeline. Listens for both LLMContextFrame
    (emitted by the user context aggregator) and LLMMessagesAppendFrame.
    When either passes through, checks if the EmotionTracker has a pending
    intervention note and appends it as a system message.
    """

    def __init__(self, tracker: EmotionTracker, context: LLMContext, **kwargs):
        super().__init__(**kwargs)
        self._tracker = tracker
        self._context = context

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # The user context aggregator emits LLMContextFrame (not LLMMessagesAppendFrame)
        if isinstance(frame, (LLMContextFrame, LLMMessagesAppendFrame)):
            note = self._tracker.consume_note()
            if note:
                logger.info("Injecting emotion awareness note into LLM context")
                self._context.add_message({
                    "role": "system",
                    "content": note,
                })

        await self.push_frame(frame, direction)
