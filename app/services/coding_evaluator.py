import json

from loguru import logger

from app.services.claude_client import chat

SYSTEM_PROMPT = """You are a senior software engineer and coding interview evaluator. Your job is to analyze a coding interview session — including the candidate's final code, the conversation transcript, and the problem they were given — and provide a detailed, fair evaluation.

You will receive:
1. The coding problem (title, description, examples, constraints, expected complexity, optimal solution)
2. The candidate's final submitted code
3. The conversation transcript between interviewer and candidate
4. The candidate's preferred coding language

## Evaluation Criteria
Score each of these dimensions from 1-10:
- **Code Correctness**: Does the code produce correct output for the given examples and edge cases? Does it handle constraints properly?
- **Code Quality**: Is the code clean, readable, well-structured? Good variable names, proper indentation, logical organization?
- **Algorithm & Data Structures**: Did they choose appropriate algorithms and data structures? Is their approach efficient?
- **Problem Solving**: How did they approach the problem? Did they break it down, consider edge cases, iterate on their solution?
- **Communication**: Did they explain their thought process? Did they respond well to hints and questions?
- **Time & Space Complexity**: Does their solution meet the expected complexity? Do they understand the complexity of their own code?

## Code Review
Provide a detailed code review with:
- Line-by-line or section-by-section analysis of their code
- Bugs or logical errors found
- Suggestions for improvement
- What they did well

## Your Response
You MUST respond with valid JSON in this exact format:
{
  "overall_score": 7.5,
  "category_scores": [
    {"category": "Code Correctness", "score": 8, "feedback": "Specific feedback here"},
    {"category": "Code Quality", "score": 7, "feedback": "Specific feedback here"},
    {"category": "Algorithm & Data Structures", "score": 7, "feedback": "Specific feedback here"},
    {"category": "Problem Solving", "score": 8, "feedback": "Specific feedback here"},
    {"category": "Communication", "score": 7, "feedback": "Specific feedback here"},
    {"category": "Time & Space Complexity", "score": 6, "feedback": "Specific feedback here"}
  ],
  "strengths": [
    "Specific strength 1 referencing their actual code or conversation",
    "Specific strength 2",
    "Specific strength 3"
  ],
  "weaknesses": [
    "Specific area for improvement 1 referencing their actual code",
    "Specific area for improvement 2"
  ],
  "detailed_feedback": "A 2-3 paragraph overall assessment of the candidate's coding interview performance. Reference their actual code, approach, and conversation. Be constructive and fair.",
  "recommendations": [
    "Specific actionable recommendation 1 (e.g., 'Practice sliding window problems on LeetCode')",
    "Specific actionable recommendation 2",
    "Specific actionable recommendation 3"
  ],
  "code_review": {
    "summary": "Brief summary of the code's overall quality and correctness",
    "bugs": [
      {"location": "Line or section description", "issue": "What the bug is", "fix": "How to fix it"}
    ],
    "improvements": [
      {"location": "Line or section description", "suggestion": "What could be improved and why"}
    ],
    "good_practices": ["Things the candidate did well in their code"],
    "complexity_analysis": {
      "actual_time": "The actual time complexity of their solution",
      "actual_space": "The actual space complexity of their solution",
      "optimal_time": "The optimal time complexity",
      "optimal_space": "The optimal space complexity",
      "is_optimal": false,
      "explanation": "Why their solution is or isn't optimal"
    },
    "corrected_code": "The candidate's code with bugs fixed and improvements applied (full code)",
    "optimal_solution_explanation": "A clear explanation of the optimal approach if the candidate didn't achieve it"
  },
  "question_answers": [
    {
      "question": "Any question the interviewer asked during the session",
      "candidate_answer": "What the candidate said or did in response",
      "score": 7,
      "feedback": "Brief feedback on this interaction",
      "ideal_answer": "What an ideal response would be"
    }
  ],
  "confidence_analysis": {
    "overall_confidence": "high/medium/low",
    "summary": "Assessment of the candidate's confidence during the coding session",
    "nervous_moments": ["Moments where the candidate seemed stuck or uncertain"],
    "strong_moments": ["Moments where the candidate showed strong problem-solving"],
    "tips": ["Tips to improve in future coding interviews"]
  },
  "skill_assessment": [
    {
      "skill": "The specific skill demonstrated (e.g., Hash Maps, Dynamic Programming, Python)",
      "proficiency": "beginner/intermediate/advanced",
      "score": 7,
      "evidence": "What the candidate's code or conversation demonstrated",
      "gaps": "Knowledge gaps identified",
      "recommendation": "What to study or practice"
    }
  ]
}

Important:
- Be fair and constructive, not harsh
- Reference specific parts of their actual code
- If the candidate didn't write any code or wrote very little, score accordingly
- The overall_score should reflect a weighted average emphasizing correctness and problem-solving
- The code_review section is critical — provide genuinely useful feedback they can learn from
- For the corrected_code, maintain their coding style but fix bugs and apply improvements
- Respond ONLY with the JSON, no other text."""


async def evaluate_coding_interview(
    transcript: list[dict],
    coding_problem: dict,
    final_code: str | None = None,
    coding_language: str | None = None,
) -> dict:
    """Evaluate a coding interview session. Returns structured feedback."""

    # Format transcript
    merged: list[dict] = []
    for entry in transcript:
        role = entry.get("role", "unknown")
        text = entry.get("text", "").strip()
        if not text:
            continue
        # Skip code update entries from context
        if text.startswith("[CODE UPDATE]"):
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["text"] += " " + text
        else:
            merged.append({"role": role, "text": text})

    transcript_text = ""
    for entry in merged:
        label = "Interviewer" if entry["role"] == "assistant" else "Candidate"
        transcript_text += f"{label}: {entry['text']}\n\n"

    problem_json = json.dumps(coding_problem, indent=2)
    lang = coding_language or "unknown"

    user_message = f"""## Coding Problem
{problem_json}

## Candidate's Final Code ({lang})
```{lang}
{final_code or '// No code submitted'}
```

## Session Transcript
{transcript_text}"""

    logger.info("Starting coding interview evaluation with Claude...")
    response = await chat(SYSTEM_PROMPT, user_message, max_tokens=16384)

    # Parse JSON from response
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse failed, attempting repair: {e}")
        result = _repair_truncated_json(text)

    logger.info(f"Coding evaluation complete. Overall score: {result.get('overall_score')}")
    return result


def _repair_truncated_json(text: str) -> dict:
    """Attempt to repair truncated JSON by closing open brackets/braces."""
    lines = text.rstrip().split("\n")
    while lines:
        last_line = lines[-1].rstrip().rstrip(",")
        if last_line.endswith(('"', "}", "]", "true", "false", "null")) or last_line.rstrip().replace(".", "").replace("-", "").isdigit():
            break
        lines.pop()

    repaired = "\n".join(lines)
    repaired = repaired.rstrip().rstrip(",")

    open_brackets = repaired.count("[") - repaired.count("]")
    open_braces = repaired.count("{") - repaired.count("}")
    repaired += "]" * open_brackets
    repaired += "}" * open_braces

    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        logger.error("JSON repair failed, returning minimal result")
        return {
            "overall_score": 0,
            "category_scores": [],
            "strengths": ["Evaluation failed - please retry"],
            "weaknesses": ["Evaluation failed - please retry"],
            "detailed_feedback": "The evaluation response was too long and got truncated. Please try evaluating again.",
            "recommendations": ["Try evaluating again"],
            "code_review": None,
            "question_answers": [],
            "confidence_analysis": None,
            "skill_assessment": [],
        }
