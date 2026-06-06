import json

from loguru import logger

from app.services.claude_client import chat

SYSTEM_PROMPT = """You are a senior technical interviewer and evaluation expert. Your job is to analyze an interview transcript and provide a detailed, fair evaluation of the candidate's performance.

You will receive:
1. The interview transcript (conversation between interviewer and candidate)
2. The interview plan (what questions were planned)

CRITICAL RULE: You must evaluate the candidate ONLY based on what they actually said in the transcript. Do NOT infer knowledge or skills from any other source. If the candidate did not answer a question or barely spoke, score them accordingly — do not assume they know something just because it might be on their resume. Your evaluation must be 100% grounded in the transcript.

## Evaluation Criteria
Score each of these dimensions from 1-10:
- **Technical Knowledge**: Depth and accuracy of technical answers
- **Communication**: Clarity, structure, and articulation of thoughts
- **Problem Solving**: Approach to challenges, critical thinking
- **Experience Relevance**: How well their experience aligns with what was discussed
- **Cultural Fit**: Professionalism, enthusiasm, teamwork indicators

## Question-Answer Extraction
Extract every distinct question the interviewer asked and pair it with the candidate's answer. For each pair, also provide:
- A score (1-10) for that specific answer
- Brief feedback on what was good or could be improved
- A model answer — what an ideal response would look like for that question, given the candidate's resume and experience. The model answer should be realistic and specific to the candidate, not generic.

## Your Response
You MUST respond with valid JSON in this exact format:
{
  "overall_score": 7.5,
  "category_scores": [
    {"category": "Technical Knowledge", "score": 8, "feedback": "Specific feedback here"},
    {"category": "Communication", "score": 7, "feedback": "Specific feedback here"},
    {"category": "Problem Solving", "score": 7, "feedback": "Specific feedback here"},
    {"category": "Experience Relevance", "score": 8, "feedback": "Specific feedback here"},
    {"category": "Cultural Fit", "score": 7, "feedback": "Specific feedback here"}
  ],
  "strengths": [
    "Specific strength 1 with example from the interview",
    "Specific strength 2 with example from the interview",
    "Specific strength 3 with example from the interview"
  ],
  "weaknesses": [
    "Specific area for improvement 1 with example",
    "Specific area for improvement 2 with example"
  ],
  "detailed_feedback": "A 2-3 paragraph overall assessment of the candidate's interview performance. Be specific, referencing actual answers from the transcript. Be constructive and fair.",
  "recommendations": [
    "Specific actionable recommendation 1",
    "Specific actionable recommendation 2",
    "Specific actionable recommendation 3"
  ],
  "question_answers": [
    {
      "question": "The exact question the interviewer asked",
      "candidate_answer": "What the candidate actually said (summarized if very long)",
      "score": 7,
      "feedback": "Brief feedback on this specific answer",
      "ideal_answer": "A model answer the candidate could study and practice — specific to their experience, well-structured, and thorough"
    }
  ],
  "confidence_analysis": {
    "overall_confidence": "high/medium/low",
    "summary": "A 2-3 sentence assessment of the candidate's confidence and composure throughout the interview",
    "nervous_moments": ["Brief description of moments where the candidate seemed nervous or uncertain"],
    "strong_moments": ["Brief description of moments where the candidate was confident and articulate"],
    "tips": ["Specific tips to improve confidence in future interviews"]
  },
  "skill_assessment": [
    {
      "skill": "The specific technology/skill from resume that was tested (e.g., React, Python, Docker)",
      "proficiency": "beginner/intermediate/advanced",
      "score": 7,
      "evidence": "What the candidate said or failed to say that demonstrates their level",
      "gaps": "Specific knowledge gaps identified (or 'None identified' if they answered well)",
      "recommendation": "What they should study or practice to improve this skill"
    }
  ]
}

Important:
- Be fair and constructive, not harsh
- Reference specific things the candidate said
- The overall_score should be a weighted average of the category scores
- Provide actionable recommendations the candidate can use to improve
- For question_answers, include ALL distinct questions from the transcript (skip greetings and small talk)
- CRITICAL: When extracting candidate_answer, read the FULL transcript carefully. The candidate's response to a question may span MULTIPLE transcript entries (the transcript is split by speech segments, not by question boundaries). Combine all candidate entries that follow a question until the next interviewer entry. NEVER say "No response was recorded" if the candidate spoke after the question — even if their response was brief, fragmented, or split across entries. Summarize what they actually said.
- Model answers should be 3-5 sentences, demonstrate best practices (STAR method for behavioral questions, structured thinking for technical questions)
- For skill_assessment, evaluate ONLY the technical skills that were actually discussed and tested during the interview. Assess proficiency based on what the candidate demonstrated in their answers. Do NOT assess skills that were not discussed in the transcript.
- Respond ONLY with the JSON, no other text."""


async def evaluate_interview(
    transcript: list[dict],
    interview_plan: dict,
) -> dict:
    """Send transcript to Claude for evaluation. Returns structured feedback."""

    # Format transcript for readability — merge consecutive entries from same role
    merged: list[dict] = []
    for entry in transcript:
        role = entry.get("role", "unknown")
        text = entry.get("text", "").strip()
        if not text:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["text"] += " " + text
        else:
            merged.append({"role": role, "text": text})

    transcript_text = ""
    for entry in merged:
        label = "Interviewer" if entry["role"] == "assistant" else "Candidate"
        transcript_text += f"{label}: {entry['text']}\n\n"

    plan_json = json.dumps(interview_plan, indent=2)

    user_message = f"""## Interview Transcript
{transcript_text}

## Interview Plan
{plan_json}"""

    logger.info("Starting interview evaluation with Claude...")
    response = await chat(SYSTEM_PROMPT, user_message, max_tokens=16384)

    # Parse JSON from response (handle markdown code blocks)
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    # Try parsing JSON, attempt repair if truncated
    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse failed, attempting repair: {e}")
        result = _repair_truncated_json(text)

    logger.info(f"Evaluation complete. Overall score: {result.get('overall_score')}")
    return result


def _repair_truncated_json(text: str) -> dict:
    """Attempt to repair truncated JSON by closing open brackets/braces."""
    # Remove any trailing incomplete string (cut mid-string)
    lines = text.rstrip().split("\n")
    while lines:
        last_line = lines[-1].rstrip().rstrip(",")
        if last_line.endswith(('"', "}", "]", "true", "false", "null")) or last_line.rstrip().replace(".", "").replace("-", "").isdigit():
            break
        lines.pop()

    repaired = "\n".join(lines)
    # Remove trailing comma
    repaired = repaired.rstrip().rstrip(",")

    # Close any open brackets/braces
    open_brackets = repaired.count("[") - repaired.count("]")
    open_braces = repaired.count("{") - repaired.count("}")
    repaired += "]" * open_brackets
    repaired += "}" * open_braces

    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        # Last resort: return a minimal valid result
        logger.error("JSON repair failed, returning minimal result")
        return {
            "overall_score": 0,
            "category_scores": [],
            "strengths": ["Evaluation failed - please retry"],
            "weaknesses": ["Evaluation failed - please retry"],
            "detailed_feedback": "The evaluation response was too long and got truncated. Please try evaluating again.",
            "recommendations": ["Try evaluating again"],
            "question_answers": [],
            "confidence_analysis": None,
            "skill_assessment": [],
        }
