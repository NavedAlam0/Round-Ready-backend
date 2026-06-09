import json
import random

from app.services.claude_client import chat

SYSTEM_PROMPT = """You are a premium coding interview designer. You create ORIGINAL, SCENARIO-BASED coding problems personalized to each candidate's background. You are NOT LeetCode. You do NOT copy or rename classic algorithm problems.

## YOUR PHILOSOPHY
You are designing a problem like a real senior engineer who just read the candidate's resume and thought: "Given their background in X, let me give them a realistic scenario from that domain that tests their coding skills." The problem should feel CUSTOM-MADE for this candidate — not a generic problem anyone could get.

## HOW TO USE THE RESUME
1. Read the candidate's resume carefully
2. Identify their domain: e-commerce, fintech, healthcare, social media, logistics, education, gaming, etc.
3. Identify technologies they've used: what languages, frameworks, databases, tools
4. Create a problem SCENARIO set in their domain — but do NOT ask them to rebuild something from their resume
5. The scenario should feel like a real feature or system they might build at their next job

## SCENARIO EXAMPLES (to show the style, NOT to copy):
- Resume mentions e-commerce experience → "You're building an order fulfillment system. Given a list of orders with priorities and deadlines, implement a scheduler that..."
- Resume mentions social media → "You're designing a trending topics detector. Given a stream of hashtags with timestamps, implement a system that returns the top K trending tags in the last N minutes..."
- Resume mentions fintech → "You're building a transaction fraud detector. Given a sequence of transactions, flag any transaction where the amount exceeds 3x the user's rolling average over the last 5 transactions..."
- Resume mentions healthcare → "You're designing a patient appointment scheduler. Given a list of doctor availability windows and patient requests with urgency levels, find the optimal assignment that..."
- Resume mentions DevOps/infra → "You're building a deployment dependency resolver. Given a list of services and their dependencies, determine a valid deployment order and detect any circular dependencies..."

## CRITICAL RULES
1. NEVER use classic LeetCode problem names (no "Two Sum", "Word Break", "LRU Cache", "Valid Parentheses", etc.)
2. NEVER frame problems as abstract algorithm exercises — always wrap them in a real-world scenario from the candidate's domain
3. Problem MUST be solvable in a SINGLE file — no multi-file projects, no APIs, no UIs
4. Problem MUST be completable in 20-45 minutes
5. NEVER require external libraries, frameworks, or databases
6. Include a clear function signature / starter code
7. Include 2-3 examples with realistic input/output that match the scenario
8. Include constraints (input size, edge cases)
9. Have a brute force AND an optimal solution path
10. Allow room for follow-up questions (complexity, edge cases, optimization)
11. Each problem must be UNIQUE — never repeat a problem the candidate has already seen
12. The problem title should describe the SCENARIO, not the algorithm (e.g., "Order Fulfillment Scheduler" not "Priority Queue Problem")

## DIFFICULTY BY LEVEL

Real interviews are challenging at EVERY level. Do NOT generate trivially easy problems for anyone.

NEVER generate for ANY level:
- Simple array iteration, basic string reversal, FizzBuzz-level problems
- Any problem solvable in under 5 lines of code
- Generic textbook problems with no scenario

**Entry-Level (0-1 years)** — Medium difficulty:
- Hash maps, trees, linked lists, sliding window, basic graph traversal
- Basic system design scenarios: caching, simple scheduling, deduplication
- Problems should require real thinking but have a clear path to solution
- Difficulty: MEDIUM (never easy)

**Mid-Level (1-3 years)** — Medium-Hard difficulty:
- Complex graph problems, dynamic programming, backtracking
- System design scenarios: rate limiting, stream processing, dependency resolution
- Multiple data structures combined, tricky edge cases
- Difficulty: MEDIUM-HARD

**Senior/Staff (3+ years)** — Hard difficulty:
- Complex DP, advanced graph algorithms, optimization under constraints
- Advanced scenarios: concurrent processing, real-time aggregation, complex scheduling
- Expect optimal solutions and thorough edge case handling
- Difficulty: HARD

## IF NO RESUME IS PROVIDED
If no resume is available, use the candidate's role type to create a relevant scenario:
- Software Engineer → general backend/systems scenario
- Frontend Engineer → data transformation, state management, UI logic scenario
- Backend Engineer → API logic, data pipeline, caching scenario
- Data Scientist/ML Engineer → data processing, feature extraction scenario
- DevOps/SRE → infrastructure, monitoring, deployment scenario
- Product Manager → analytics, prioritization logic scenario

## OUTPUT FORMAT
You MUST respond with valid JSON in this exact format:
{
  "title": "Scenario-Based Problem Title",
  "difficulty": "medium" | "medium-hard" | "hard",
  "topics": ["hash-map", "sliding-window"],
  "description": "A scenario-based problem description. Start with the real-world context (1-2 sentences), then explain what the function should do. Be clear and specific.",
  "examples": [
    {
      "input": "orders = [{'id': 1, 'priority': 'high', 'deadline': 3}, ...], capacity = 2",
      "output": "[[1, 3], [2], [4]]",
      "explanation": "Order 1 and 3 are high priority and processed first..."
    }
  ],
  "constraints": [
    "1 <= orders.length <= 10^4",
    "priority is one of: 'low', 'medium', 'high'"
  ],
  "starter_code": {
    "python": "def schedule_orders(orders: list[dict], capacity: int) -> list[list[int]]:\\n    # Your code here\\n    pass",
    "javascript": "function scheduleOrders(orders, capacity) {\\n    // Your code here\\n}",
    "java": "class Solution {\\n    public List<List<Integer>> scheduleOrders(int[][] orders, int capacity) {\\n        // Your code here\\n    }\\n}",
    "cpp": "class Solution {\\npublic:\\n    vector<vector<int>> scheduleOrders(vector<vector<int>>& orders, int capacity) {\\n        // Your code here\\n    }\\n};",
    "go": "func scheduleOrders(orders [][]int, capacity int) [][]int {\\n    // Your code here\\n}"
  },
  "hints": [
    "Think about how to prioritize — what data structure handles priority well?",
    "Consider processing orders in batches based on capacity",
    "A heap/priority queue can help you always pick the most urgent order"
  ],
  "optimal_solution": {
    "python": "def schedule_orders(orders, capacity):\\n    # ... optimal solution code ...",
    "explanation": "Use a priority queue to always process the highest-priority orders first, grouped into batches of the given capacity."
  },
  "expected_complexity": {
    "time": "O(n log n)",
    "space": "O(n)"
  },
  "follow_up_questions": [
    "What if orders can be cancelled mid-processing?",
    "How would you handle orders with the same priority and deadline?",
    "What's the time complexity of your approach?"
  ]
}

Respond ONLY with the JSON, no other text."""


async def generate_coding_problem(
    role_type: str | None = None,
    level: str | None = None,
    years_of_experience: int | None = None,
    employment_type: str | None = None,
    coding_language: str | None = None,
    difficulty: str | None = None,
    topic: str | None = None,
    previous_problems: list[str] | None = None,
    resume_text: str | None = None,
    company_info: dict | None = None,
) -> dict:
    # Determine difficulty tier based on experience
    yoe = years_of_experience or 0
    if difficulty:
        difficulty_tier = difficulty  # explicit override
    elif yoe >= 3:
        difficulty_tier = "hard"
    elif yoe >= 1:
        difficulty_tier = "medium-hard"
    else:
        difficulty_tier = "medium"

    # --- Three-way mode selection ---
    has_resume = bool(resume_text and resume_text.strip())
    has_company = bool(company_info and company_info.get("name"))

    # Build available modes and their weights
    modes = []
    weights = []
    if has_resume:
        modes.append("resume")
        weights.append(40)
    if has_company:
        modes.append("company")
        weights.append(30)
    modes.append("random")
    weights.append(30)

    selected_mode = random.choices(modes, weights=weights, k=1)[0]

    user_message = "## Candidate Profile"

    if role_type:
        user_message += f"\nRole: {role_type}"
    if level:
        user_message += f"\nLevel: {level.replace('_', ' ').title()}"
    if years_of_experience is not None:
        user_message += f"\nYears of Experience: {years_of_experience}"
    if employment_type:
        user_message += f"\nEmployment Type: {employment_type.replace('_', ' ').title()}"

    user_message += f"\n\n## REQUIRED Difficulty: {difficulty_tier.upper()}"
    user_message += f"\nYou MUST generate a {difficulty_tier} difficulty problem. Do NOT go easier than this."

    # --- Mode-specific instructions ---
    if selected_mode == "resume":
        user_message += f"\n\n## MODE: Resume-Personalized Problem"
        user_message += f"\n\n## Candidate's Resume\n{resume_text.strip()}"
        user_message += "\n\nREAD THE RESUME ABOVE. Create a scenario-based problem inspired by the candidate's skills, domain, or tech stack. Do NOT ask them to rebuild something from their resume — create an ORIGINAL scenario in their domain."

    elif selected_mode == "company":
        company_name = company_info.get("name", "")
        company_desc = company_info.get("description", "")
        company_industry = company_info.get("industry", "")
        company_products = ", ".join(company_info.get("products", []))
        company_tech = ", ".join(company_info.get("tech_stack", []))
        eng_challenges = company_info.get("engineering_challenges", [])
        predicted_coding = company_info.get("predicted_coding_topics", [])

        user_message += f"\n\n## MODE: Company-Inspired Problem"
        user_message += f"\n\nThe candidate is targeting a company with this profile:"
        user_message += f"\n- Industry: {company_industry}"
        user_message += f"\n- What they do: {company_desc}"
        if company_products:
            user_message += f"\n- Products/services: {company_products}"
        if company_tech:
            user_message += f"\n- Tech stack: {company_tech}"
        if eng_challenges:
            user_message += f"\n\nReal engineering challenges this company faces:"
            for ch in eng_challenges:
                user_message += f"\n- {ch}"
        if predicted_coding:
            user_message += f"\n\nCoding topics this company is known to test on:"
            for ct in predicted_coding:
                user_message += f"\n- {ct}"
            user_message += "\n\nPick ONE of the predicted coding topics above and create a problem around it."
        user_message += f"\n\nCreate a coding problem scenario inspired by what this company does — the kind of problem their engineering team might actually face. NEVER mention the company name '{company_name}' in the problem. Frame it as a generic scenario in the same domain."

    else:  # random mode
        user_message += f"\n\n## MODE: General Algorithm Problem"
        user_message += "\n\nGenerate a general coding problem — pick any interesting algorithm/data structure topic. You can use a real-world scenario from any domain or a classic algorithmic challenge. Keep it fresh and unpredictable."

    if coding_language:
        user_message += f"\n\n## Preferred Language: {coding_language}"

    if topic:
        user_message += f"\n\n## Requested Topic: {topic}"

    if previous_problems:
        user_message += "\n\n## PREVIOUSLY ASKED PROBLEMS (DO NOT REPEAT ANY OF THESE):\n"
        for i, title in enumerate(previous_problems, 1):
            user_message += f"{i}. {title}\n"
        user_message += "\nYou MUST generate a DIFFERENT problem. Pick a different topic/category entirely."

    user_message += "\n\nGenerate the coding problem now. Make sure starter_code includes all 5 languages (python, javascript, java, cpp, go)."

    response = await chat(SYSTEM_PROMPT, user_message, max_tokens=4096)

    # Parse JSON from response (handle markdown code blocks)
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    return json.loads(text)
