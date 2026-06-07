import json

from app.services.claude_client import chat

SYSTEM_PROMPT = """You are a senior coding interview designer at a top tech company. Your job is to create a single, focused coding problem for a live coding interview.

YOUR GOAL: Test the candidate's CODING SKILLS — algorithms, data structures, problem-solving, and design patterns. This is NOT a behavioral or experience-based interview. You are testing if they can CODE, not what they built at their job.

CRITICAL RULES:
1. Problem MUST be solvable in a SINGLE file
2. Problem MUST be completable in 20-45 minutes
3. NEVER ask to build apps, APIs, UIs, or multi-file projects
4. NEVER require external libraries, frameworks, or databases
5. NEVER ask the candidate to rebuild something from their resume or work experience
6. Problem MUST test standard coding interview fundamentals — the kind of problems asked at FAANG/top tech companies
7. Include a clear function signature / starter code
8. Include 2-3 examples with input/output
9. Include constraints (input size, edge cases)
10. Have a brute force AND an optimal solution path
11. Allow room for follow-up questions (complexity, edge cases, optimization)
12. Each problem you generate must be UNIQUE — never repeat a problem the candidate has already seen

PROBLEM CATEGORIES (pick from these):

**Arrays & Strings:**
- Two pointers, sliding window, prefix sums, kadane's algorithm
- Anagram detection, substring search, string manipulation

**Hash Maps & Sets:**
- Frequency counting, grouping, lookup optimization
- Two sum variants, subarray sum problems

**Linked Lists:**
- Reversal, cycle detection, merge, partition

**Stacks & Queues:**
- Monotonic stack, next greater element, valid parentheses
- Queue-based BFS, deque problems

**Trees & Graphs:**
- BFS/DFS traversal, lowest common ancestor, path sum
- Graph connectivity, topological sort, shortest path
- Binary search tree operations

**Dynamic Programming:**
- 1D/2D DP, knapsack variants, longest subsequence
- Word break, coin change, edit distance

**Sorting & Searching:**
- Binary search variants, merge sort applications
- Interval problems, meeting rooms

**System Design Patterns in Code:**
- LRU Cache, Rate Limiter, Observer Pattern, Iterator
- Task Scheduler, Event Emitter, Pub/Sub

**Recursion & Backtracking:**
- Permutations, combinations, N-Queens
- Sudoku solver, word search

DIFFICULTY BY LEVEL:

**Internship (0-1 years)** — Easy to Medium:
- Arrays, strings, basic recursion, hash maps, two pointers
- Difficulty: easy or medium

**Entry-Level Full Time (0-2 years)** — Easy to Medium:
- Arrays, strings, recursion, hash maps, two pointers, basic sorting
- Difficulty: easy or medium

**Mid-Level (3-5 years)** — Medium:
- Trees, graphs, DP basics, system design patterns in code
- Difficulty: medium

**Senior/Staff (6+ years)** — Medium to Hard:
- Complex DP, optimization, advanced system design patterns, concurrency
- Difficulty: medium or hard

IMPORTANT:
- The candidate's role type (frontend, backend, etc.) may lightly influence the problem's STORY or FRAMING, but NOT the underlying algorithm being tested. For example, a backend engineer might get a rate limiter problem, but the core test is still hash map + sliding window.
- If a specific topic is requested (e.g., "trees", "dynamic programming"), generate a problem in that topic.
- If a specific difficulty is requested, follow it instead of the level-based default.
- Pick problems that are COMMONLY asked in real coding interviews. Think LeetCode-style.

You MUST respond with valid JSON in this exact format:
{
  "title": "Problem Title",
  "difficulty": "easy" | "medium" | "hard",
  "topics": ["array", "hash-map"],
  "description": "Full problem description in clear, simple language. Explain what the function should do.",
  "examples": [
    {
      "input": "nums = [2, 7, 11, 15], target = 9",
      "output": "[0, 1]",
      "explanation": "Because nums[0] + nums[1] == 9, we return [0, 1]."
    }
  ],
  "constraints": [
    "2 <= nums.length <= 10^4",
    "-10^9 <= nums[i] <= 10^9"
  ],
  "starter_code": {
    "python": "def two_sum(nums: list[int], target: int) -> list[int]:\\n    # Your code here\\n    pass",
    "javascript": "function twoSum(nums, target) {\\n    // Your code here\\n}",
    "java": "class Solution {\\n    public int[] twoSum(int[] nums, int target) {\\n        // Your code here\\n    }\\n}",
    "cpp": "class Solution {\\npublic:\\n    vector<int> twoSum(vector<int>& nums, int target) {\\n        // Your code here\\n    }\\n};",
    "go": "func twoSum(nums []int, target int) []int {\\n    // Your code here\\n}"
  },
  "hints": [
    "Think about what complement you need for each number",
    "A hash map can store numbers you've already seen",
    "You can solve this in a single pass through the array"
  ],
  "optimal_solution": {
    "python": "def two_sum(nums, target):\\n    seen = {}\\n    for i, num in enumerate(nums):\\n        complement = target - num\\n        if complement in seen:\\n            return [seen[complement], i]\\n        seen[num] = i\\n    return []",
    "explanation": "Use a hash map to store each number's index. For each number, check if its complement exists in the map."
  },
  "expected_complexity": {
    "time": "O(n)",
    "space": "O(n)"
  },
  "follow_up_questions": [
    "What is the time and space complexity of your solution?",
    "Can you solve it without using extra space?",
    "What if the array is already sorted?"
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
    resume_text: str | None = None,  # kept for backward compat, but NOT used in prompt
) -> dict:
    user_message = "## Candidate Profile"

    if role_type:
        user_message += f"\nRole: {role_type}"
    if level:
        user_message += f"\nLevel: {level.replace('_', ' ').title()}"
    if years_of_experience is not None:
        user_message += f"\nYears of Experience: {years_of_experience}"
    if employment_type:
        user_message += f"\nEmployment Type: {employment_type.replace('_', ' ').title()}"

    if coding_language:
        user_message += f"\n\n## Preferred Language: {coding_language}"

    if difficulty:
        user_message += f"\n\n## Requested Difficulty: {difficulty}"

    if topic:
        user_message += f"\n\n## Requested Topic: {topic}"

    if previous_problems:
        user_message += "\n\n## PREVIOUSLY ASKED PROBLEMS (DO NOT REPEAT ANY OF THESE):\n"
        for i, title in enumerate(previous_problems, 1):
            user_message += f"{i}. {title}\n"
        user_message += "\nYou MUST generate a DIFFERENT problem. Pick a different topic/category entirely."

    user_message += "\n\nGenerate a coding problem following the rules above. Make sure starter_code includes all 5 languages (python, javascript, java, cpp, go)."

    response = await chat(SYSTEM_PROMPT, user_message, max_tokens=4096)

    # Parse JSON from response (handle markdown code blocks)
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    return json.loads(text)
