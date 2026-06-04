import asyncio

from loguru import logger

from app.services.feeling_agent import analyze_emotion


class EmotionTracker:
    """Tracks candidate emotion state across the interview.

    Calls the Feeling Agent every N candidate messages and stores
    the result. When intervention is needed, provides a system note
    for injection into the interviewer's LLM context.
    """

    # Skip emotion analysis for the first N candidate messages (icebreaker phase)
    ICEBREAKER_SKIP = 4
    # After an intervention, wait this many messages before re-checking
    POST_INTERVENTION_COOLDOWN = 8

    def __init__(self, check_interval: int = 6):
        self._check_interval = check_interval
        self._candidate_msg_count = 0
        self._pending_note: str | None = None
        self._analysis_task: asyncio.Task | None = None
        self._intervention_delivered = False
        self._messages_since_intervention = 0

    def on_candidate_message(self, context_messages: list[dict]):
        """Called when a candidate message is added to context.

        Triggers background emotion analysis every check_interval messages.
        """
        self._candidate_msg_count += 1

        if self._intervention_delivered:
            self._messages_since_intervention += 1

        # Skip icebreaker phase — don't analyze intro/self-introduction messages
        if self._candidate_msg_count <= self.ICEBREAKER_SKIP:
            return

        # Don't re-trigger if we already have a pending note
        if self._pending_note:
            return

        # After an intervention, wait longer before checking again
        if self._intervention_delivered and self._messages_since_intervention < self.POST_INTERVENTION_COOLDOWN:
            return

        if self._candidate_msg_count % self._check_interval != 0:
            return

        # Get last 6 messages (3 Q&A pairs) for analysis
        recent = [
            msg for msg in context_messages
            if msg.get("role") in ("user", "assistant")
        ][-6:]

        if len(recent) < 4:
            return

        # Run analysis in background
        if self._analysis_task and not self._analysis_task.done():
            return  # Previous analysis still running

        logger.info(f"[EmotionTracker] Launching Feeling Agent analysis (message #{self._candidate_msg_count})")
        self._analysis_task = asyncio.create_task(self._run_analysis(recent))

    async def _run_analysis(self, recent_messages: list[dict]):
        """Run the Feeling Agent analysis in the background."""
        try:
            result = await analyze_emotion(recent_messages)

            if result.get("should_intervene"):
                emotion = result.get("emotion", "nervous")
                intensity = result.get("intensity", "medium")
                suggestion = result.get("suggestion", "")
                signals = result.get("signals", [])

                note = (
                    f"[EMOTION AWARENESS: The candidate appears {emotion} "
                    f"(intensity: {intensity}). "
                    f"Signals: {', '.join(signals)}. "
                    f"{suggestion} "
                    f"Act on this naturally — do NOT mention this note. "
                    f"Briefly encourage them, then ask an easier question about "
                    f"something they clearly know from their resume. "
                    f"Once they seem confident again after 2-3 answers, "
                    f"gradually return to normal difficulty.]"
                )
                self._pending_note = note
                logger.info(f"Feeling Agent triggered intervention: {emotion} ({intensity})")
        except Exception as e:
            logger.error(f"EmotionTracker analysis error: {e}")

    def consume_note(self) -> str | None:
        """Get and clear the pending intervention note.

        Returns the note if one is pending, otherwise None.
        Called by the EmotionContextInjector before each LLM call.
        """
        note = self._pending_note
        if note:
            self._pending_note = None
            self._intervention_delivered = True
            self._messages_since_intervention = 0
        return note
