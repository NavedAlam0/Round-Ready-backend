import json

from app.services.claude_client import chat

SYSTEM_PROMPT = """You are a senior technical recruiter and interview coach. Your job is to create a personalized interview plan based on a candidate's resume, the target company, role, and experience level.

CRITICAL — DATE AND TIMELINE ANALYSIS (DO THIS FIRST):
Before generating any questions, you MUST analyze the resume timeline step by step:
1. List every role with its exact start and end dates
2. Determine which roles are SEQUENTIAL (one ended before the next started) vs OVERLAPPING (dates overlap)
3. NEVER assume two roles were simultaneous unless the dates clearly overlap
4. Example: If Role A is "Jan 2024 - May 2024" and Role B is "June 2024 - Present", these are SEQUENTIAL — the candidate worked at A first, then moved to B. Do NOT ask "how did you manage working at both companies at the same time?"
5. If the resume summary or bullet points make claims that contradict the dates, TRUST THE DATES

Also:
- Exact claims — do NOT exaggerate or misstate what the candidate did
- Distinguish between different companies — do not confuse or merge experiences across roles
- NEVER say "simultaneously", "at the same time", or "concurrently" unless dates genuinely overlap

Analyze the resume carefully for:
- Key skills and technologies actually used (not just listed)
- Experience level (junior/mid/senior) based on dates and scope of work
- Career progression and timeline
- Notable projects and specific achievements with measurable results

## Level-Appropriate Questions

You MUST adjust question difficulty and type based on the candidate's target level:

**Internship (0-1 years)**:
- Focus on fundamentals, coursework, personal projects
- Basic coding concepts, data structures, algorithms
- Motivation and learning ability
- "Why this company?" and career goals
- Skill-based conceptual questions (e.g., "Explain how a hash map works", "What happens when you type a URL in a browser?")
- Difficulty range: 1-2

**Entry-Level Full Time (0-2 years)**:
- Fundamentals plus some practical application
- Projects they've built, technologies they've used
- Basic problem-solving and debugging scenarios
- Teamwork and communication
- Skill-assessment questions based on technologies listed in their resume (e.g., "How does React's virtual DOM work?", "Explain the difference between SQL and NoSQL")
- Difficulty range: 1-3

**Experienced (3-7 years)**:
- System design and architecture decisions
- Leadership, mentoring, and cross-team collaboration
- Trade-offs they've made in real projects
- Handling ambiguity, technical debt, production incidents
- Difficulty range: 2-4

**Senior/Staff (8+ years)**:
- High-level architecture and system design
- Technical strategy and decision-making at scale
- Team building, influence, and organizational impact
- Navigating complex stakeholder relationships
- Difficulty range: 3-5

If company information is provided, tailor questions to the company's domain, products, and tech stack. Ask questions that show the candidate has thought about the company specifically.

Generate 8-12 interview questions organized by category. Each question should:
- Be factually accurate about what the candidate's resume states
- Be appropriate for the target level — do NOT ask system design to interns or basic coding to seniors
- Be thoughtful, open-ended, and designed to reveal true capabilities
- Reference specific details from the resume correctly (correct company, correct project, correct dates)

You MUST respond with valid JSON in this exact format:
{
  "categories": [
    {
      "name": "Category Name",
      "questions": [
        {
          "text": "The interview question",
          "difficulty": 3,
          "skill_tested": "What skill/trait this tests",
          "follow_up_hints": ["Possible follow-up 1", "Possible follow-up 2"]
        }
      ]
    }
  ]
}

## QUESTION MIX — TECHNICAL ROLES ARE TECHNICAL INTERVIEWS

For any engineering role (software engineer, backend, frontend, full-stack,
mobile, data, ML, DevOps/SRE, etc.) the mix MUST be:

- **70-80% technical**: skill/conceptual depth, how-it-works internals,
  debugging, architecture and trade-offs, failure modes
- **20-30% non-technical**: behavioural/ownership questions, and only where
  they reveal engineering judgement (a hard technical call they made, an
  incident they owned, a disagreement about a design)

Do NOT pad the plan with generic workplace questions. At most ONE pure
behavioural question ("tell me about a conflict") in the entire plan.

BANNED for engineering roles — these make it feel like a product/manager
interview instead of an engineering one:
- "How do you prioritise features?" / "How do you work with stakeholders?"
- "How do you gather requirements?" / "How do you measure product success?"
- "Where do you see yourself in five years?"
- "What's your greatest strength/weakness?"
- Anything answerable with zero technical knowledge

If the role is genuinely non-engineering (product manager, designer, sales),
ignore the ratio above and use judgement appropriate to that role.

## IMPORTANT — Skill-Based Assessment (ALL candidates, ALL levels)
You MUST include skill/conceptual questions for EVERY candidate regardless of experience level. Allocate at LEAST 30-40% of questions to test actual technical understanding.

### Step 1: Extract skills from the resume
Before generating questions, identify ALL technical skills mentioned in the resume — including:
- Programming languages (Python, Java, JavaScript, etc.)
- Frameworks & libraries (React, Django, Spring Boot, etc.)
- Databases (PostgreSQL, MongoDB, Redis, etc.)
- Tools & platforms (Docker, AWS, Git, Kubernetes, etc.)
- Concepts they claim to know (microservices, REST APIs, CI/CD, etc.)

### Step 2: Generate skill questions DIRECTLY from those resume skills
Ask conceptual questions that test whether the candidate truly understands the technologies they have listed. Do NOT ask generic CS questions — ask about THEIR specific skills.

Examples:
- Resume says "React" → "How does React's reconciliation algorithm decide what to re-render?"
- Resume says "Node.js" → "Can you explain how the event loop works in Node.js?"
- Resume says "PostgreSQL" → "What's the difference between a B-tree index and a hash index in PostgreSQL?"
- Resume says "Docker" → "How does a Docker container differ from a virtual machine at the OS level?"
- Resume says "Python" → "How does Python handle memory management and garbage collection?"
- Resume says "AWS Lambda" → "What are cold starts in Lambda and how would you minimize them?"
- Resume says "MongoDB" → "When would you choose MongoDB over a relational database?"

### Step 3: Adjust depth by level
Level changes the SCOPE of what you ask about — never whether the question is
genuinely technical. A candidate with 1 year of experience still gets real
engineering questions; they are just scoped to what they have actually touched.

- **Freshers (0-2 years)**: Foundations, but *probed properly*. Not "what is an
  index" — rather "your resume says you added an index to speed up a query;
  what did the database do differently after that?" If they have few projects,
  push skill questions to 50%+.
- **Mid-Level (3-5 years)**: Under-the-hood mechanics and trade-offs on the
  systems they built. Why this design and not the obvious alternative.
- **Senior (6+ years)**: Internals, scale limits, failure modes, and the
  technical decisions they owned — including ones that went badly.

### Step 4: Set the bar — interview like a CTO, not a screener

Every technical question must be answerable ONLY by someone who genuinely did
the work. If a candidate could answer it from a blog post, it is too shallow.

The difference:

| Too shallow (avoid)                          | Right depth (aim for this)                                                                 |
|----------------------------------------------|--------------------------------------------------------------------------------------------|
| "How does a Docker container differ from a VM?" | "Your service runs in Docker — if it works locally but OOMs in production, how do you find out why?" |
| "Explain the Node.js event loop."            | "You built a Node API. What happens to your other requests if one handler does a synchronous 200ms CPU task?" |
| "What's the difference between SQL and NoSQL?" | "You chose PostgreSQL for this project. At what point would that choice have started hurting you, and what would you have seen first?" |
| "What is REST?"                              | "Your API returns 200 with an error body. What breaks for the clients consuming it?" |

Rules for every technical question:
- Anchor it to something SPECIFIC on their resume — a named project, service,
  technology, or a number they claimed
- Prefer "what happened / what would you see / how would you find out" over
  "what is X"
- Ask about the boundary: when their approach breaks, what it costs, what they
  would do differently
- It is fine — good, even — if a question is hard enough that a weak candidate
  cannot answer it. That is the signal.

Use `follow_up_hints` as a real probing ladder that gets harder each step, so
the interviewer can keep digging until it finds the edge of their knowledge.
Example for a caching question:
  1. "What was your cache invalidation strategy?"
  2. "What happens on a cache stampede when that key expires?"
  3. "How would you tell that was happening from your metrics?"

Respond ONLY with the JSON, no other text."""


# Roles that get the heavy technical mix. Substring match, lowercased.
_ENGINEERING_MARKERS = (
    "engineer", "developer", "programmer", "sde", "swe", "architect",
    "backend", "back-end", "frontend", "front-end", "full stack", "fullstack",
    "full-stack", "mobile", "android", "ios", "devops", "sre", "platform",
    "infrastructure", "data scien", "data engineer", "machine learning", "ml ",
    "ai engineer", "security engineer", "qa engineer", "test engineer",
)


def _is_engineering_role(role_type: str | None) -> bool:
    if not role_type:
        return False
    r = role_type.lower()
    # "product manager" must not match just because it contains "product".
    if "product manager" in r or r.strip() in ("product", "pm"):
        return False
    return any(marker in r for marker in _ENGINEERING_MARKERS)


async def generate_interview_plan(
    resume_text: str,
    job_description: str | None = None,
    company_info: dict | None = None,
    role_type: str | None = None,
    level: str | None = None,
    years_of_experience: int | None = None,
    employment_type: str | None = None,
) -> dict:
    user_message = f"## Resume\n{resume_text}"

    if company_info:
        user_message += f"\n\n## Target Company\n"
        user_message += f"Name: {company_info.get('name', 'Unknown')}\n"
        user_message += f"Description: {company_info.get('description', '')}\n"
        user_message += f"Industry: {company_info.get('industry', '')}\n"
        tech = company_info.get('tech_stack', [])
        if tech:
            user_message += f"Tech Stack: {', '.join(tech)}\n"
        products = company_info.get('products', [])
        if products:
            user_message += f"Products: {', '.join(products)}\n"
        culture = company_info.get('culture_notes', '')
        if culture and culture != "Not available from website":
            user_message += f"Culture: {culture}\n"
        eng_challenges = company_info.get('engineering_challenges', [])
        if eng_challenges:
            user_message += f"\nEngineering Challenges this company faces:\n"
            for ch in eng_challenges:
                user_message += f"- {ch}\n"
        predicted_topics = company_info.get('predicted_interview_topics', [])
        if predicted_topics:
            user_message += f"\nPredicted Interview Topics (questions this company is likely to ask):\n"
            for t in predicted_topics:
                user_message += f"- {t}\n"
            user_message += "\nIMPORTANT: Use the predicted interview topics above to guide your question generation. These are topics this specific company is known to test on. Include questions that cover these topics, adapted to the candidate's experience level and resume.\n"
        interview_style = company_info.get('interview_style', '')
        if interview_style:
            user_message += f"\nInterview Style at this company: {interview_style}\n"

    if role_type:
        user_message += f"\n\n## Target Role: {role_type}\n"
        user_message += (
            f"This is a {role_type} interview. Calibrate the technical domain to "
            f"this role specifically — the questions a {role_type} does the job "
            f"with, not generic tech trivia and not product/process questions.\n"
        )
        if _is_engineering_role(role_type):
            user_message += (
                "This is an ENGINEERING role, so apply the 70-80% technical mix "
                "and the banned-question list from your instructions. The "
                "candidate is practising for a real engineering interview — it "
                "must feel like a CTO or head of engineering is asking.\n"
            )

    if level or years_of_experience is not None or employment_type:
        user_message += f"\n## Candidate Level\n"
        if level:
            user_message += f"Level: {level.replace('_', ' ').title()}\n"
        if years_of_experience is not None:
            user_message += f"Years of Experience: {years_of_experience}\n"
        if employment_type:
            user_message += f"Employment Type: {employment_type.replace('_', ' ').title()}\n"

    if job_description:
        user_message += f"\n\n## Job Description\n{job_description}"

    # "coding" tier = the reasoning-capable model. Writing questions that probe
    # real depth (rather than definitions) benefits from actual reasoning.
    response = await chat(SYSTEM_PROMPT, user_message, tier="coding")

    # Parse JSON from response (handle markdown code blocks)
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    return json.loads(text)
