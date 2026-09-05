# RoundReady Backend

FastAPI backend for the RoundReady AI Interview Bot. Handles user authentication, interview management, resume parsing, interview planning, and post-interview evaluation.

## Tech Stack

- **Framework:** FastAPI (async)
- **Database:** PostgreSQL (AWS RDS) via SQLAlchemy + asyncpg
- **Authentication:** Firebase Admin SDK
- **AI:** Anthropic Claude API (interview planning & evaluation)
- **Voice:** Daily.co (room creation & tokens)
- **Resume Parsing:** pdfplumber
- **Python:** 3.12

## Project Structure

```
backend/
├── app/
│   ├── main.py                  # FastAPI app, lifespan, CORS
│   ├── config.py                # Settings (from .env)
│   ├── database.py              # SQLAlchemy async engine & session
│   ├── models/
│   │   ├── user.py              # User model (Firebase auth)
│   │   ├── interview.py         # Interview model
│   │   └── score.py             # InterviewScore & InterviewReport models
│   ├── schemas/
│   │   ├── user.py              # User Pydantic schemas
│   │   └── interview.py         # Interview Pydantic schemas
│   ├── routers/
│   │   ├── auth.py              # Authentication (Firebase token verification)
│   │   ├── resume.py            # Resume upload & parsing
│   │   └── interview.py         # Interview CRUD, start/end, evaluation, reports
│   ├── services/
│   │   ├── claude_client.py     # Anthropic Claude API client
│   │   ├── question_planner.py  # Generate interview plans via Claude
│   │   ├── evaluator.py         # Evaluate interviews via Claude
│   │   ├── resume_parser.py     # Parse PDF resumes
│   │   ├── company_researcher.py # Company research
│   │   ├── job_scraper.py       # Job description scraping
│   └── voice/
│       ├── daily_helpers.py     # Daily.co room & token management
│       ├── interviewer_llm.py   # System prompt builder
│       ├── emotion_tracker.py   # Emotion tracking
│       └── emotion_processor.py # Emotion frame processors
├── requirements.txt
├── .env                         # Environment variables (not committed)
└── roundready-*-firebase-adminsdk-*.json  # Firebase credentials (not committed)
```

## Prerequisites

- Python 3.12
- PostgreSQL database (AWS RDS or local)
- Firebase project with Authentication enabled
- API keys for: Anthropic Claude, Deepgram, ElevenLabs, Daily.co

## Local Setup

### 1. Clone and navigate to backend

```bash
cd backend
```

### 2. Create virtual environment

```bash
python3.12 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up Firebase credentials

- Go to Firebase Console > Project Settings > Service Accounts
- Click "Generate new private key"
- Save the JSON file in the backend directory
- Note the file name for the `.env` config

### 5. Set up the database

**Option A: AWS RDS (recommended)**

1. Create an RDS PostgreSQL instance (free tier: `db.t4g.micro`)
2. Enable public access
3. Open port 5432 in the security group
4. Create the database:
   ```bash
   psql -h <your-rds-endpoint> -U postgres -c "CREATE DATABASE roundready;"
   ```

**Option B: Local PostgreSQL**

```bash
# Install PostgreSQL (macOS)
brew install postgresql
brew services start postgresql

# Create database
createdb roundready
```

### 6. Create `.env` file

```bash
cp .env.example .env  # or create manually
```

Add these variables to `.env`:

```env
# Database (AWS RDS example)
DATABASE_URL=postgresql+asyncpg://postgres:YOUR_PASSWORD@your-rds-endpoint:5432/roundready

# For local PostgreSQL use:
# DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/roundready

# Secret key for sessions
SECRET_KEY=your-secret-key-here

# API Keys
CLAUDE_API_KEY=your-anthropic-api-key
DEEPGRAM_API_KEY=your-deepgram-api-key
ELEVENLABS_API_KEY=your-elevenlabs-api-key
DAILY_API_KEY=your-daily-api-key

# Firebase
FIREBASE_CREDENTIALS_PATH=./your-firebase-adminsdk-file.json
FIREBASE_HTTP_TIMEOUT=10.0
FORCE_IPV4_HTTP=true

# Voice agent server URL
VOICE_AGENT_URL=http://localhost:8001

# CORS origins (comma separated)
CORS_ORIGINS=http://localhost:3000,https://roundready.in

# Upload directory for resumes
UPLOAD_DIR=./app/uploads
```

### 7. Create uploads directory

```bash
mkdir -p app/uploads
```

### 8. Run the server

```bash
uvicorn app.main:app --port 8000
```

The server starts on `http://localhost:8000`. Tables are auto-created on startup.

**Note:** Do NOT use `--reload` flag with asyncpg — it causes connection issues.

## API Endpoints

### Health Check
- `GET /health` — Server health status

### Authentication
- `GET /api/auth/me` — Get current user info

### Resume
- `POST /api/resume/upload` — Upload and parse a PDF resume
- `POST /api/company/research` — Research a company by name

### Interview
- `GET /api/interviews` — List all interviews for current user
- `POST /api/interview/setup` — Create a new interview
- `DELETE /api/interview/{id}` — Delete an interview
- `POST /api/interview/{id}/plan` — Generate interview plan (via Claude)
- `GET /api/interview/{id}/plan` — Get interview plan
- `POST /api/interview/{id}/start` — Start voice interview (creates Daily room, starts voice agent)
- `POST /api/interview/{id}/end` — End interview
- `GET /api/interview/{id}/transcript` — Get interview transcript
- `POST /api/interview/{id}/evaluate` — Trigger evaluation
- `GET /api/interview/{id}/report` — Get evaluation report
- `POST /api/interview/{id}/bot-callback` — Callback from voice agent (internal)

All endpoints except `/health` and `/bot-callback` require a Firebase Bearer token in the `Authorization` header.

## Environment Variables Reference

| Variable | Description | Required |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string (asyncpg format) | Yes |
| `SECRET_KEY` | App secret key | Yes |
| `CLAUDE_API_KEY` | Anthropic Claude API key | Yes |
| `DEEPGRAM_API_KEY` | Deepgram STT API key | Yes |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS API key | Yes |
| `DAILY_API_KEY` | Daily.co API key | Yes |
| `FIREBASE_CREDENTIALS_PATH` | Path to Firebase service account JSON | Yes |
| `FIREBASE_CREDENTIALS_JSON` | Firebase credentials as JSON string (alternative to path) | No |
| `FIREBASE_HTTP_TIMEOUT` | Timeout for Firebase HTTP calls (default: 10.0) | No |
| `FORCE_IPV4_HTTP` | Force IPv4 for HTTP requests (default: true) | No |
| `VOICE_AGENT_URL` | Voice agent server URL (default: http://localhost:8001) | No |
| `CORS_ORIGINS` | Comma-separated allowed origins | No |
| `UPLOAD_DIR` | Resume upload directory (default: ./app/uploads) | No |

## Architecture

### How it works

1. **User signs up/logs in** via Firebase Authentication (Google or email/password)
2. **Frontend sends Firebase ID token** with every API request
3. **Backend verifies the token** using Firebase Admin SDK
4. **User uploads resume** — parsed with pdfplumber, text extracted
5. **User sets up interview** — role, level, company info, job description
6. **Backend generates interview plan** — sends context to Claude, gets structured question plan
7. **Backend starts voice interview** — creates a Daily.co room, calls the voice agent server to start the bot
8. **Voice agent runs the interview** — Pipecat pipeline with Deepgram STT, Claude LLM, ElevenLabs TTS
9. **Voice agent sends transcript back** via `/bot-callback` when interview ends
10. **Backend evaluates the interview** — sends transcript to Claude for scoring and feedback
11. **User views report** — scores, strengths, weaknesses, recommendations

### Voice Agent Communication

The backend communicates with the voice agent server (separate service on port 8001) via HTTP:

- `POST /start-bot` — Start a voice bot for an interview
- `POST /stop-bot/{interview_id}` — Stop a running bot

The voice agent calls back to the backend when done:

- `POST /api/interview/{id}/bot-callback` — Sends transcript, authenticated via `X-Bot-Secret` header (Daily API key)

## Common Issues

### Server hangs on startup
- Make sure you're NOT using `--reload` flag
- Check that your database is accessible
- If using AWS RDS, ensure public access is enabled and security group allows port 5432

### Firebase token verification is slow
- First request may be slow (~10-40s) as Firebase downloads Google's signing keys
- Subsequent requests are fast (keys are cached in memory)
- Set `FORCE_IPV4_HTTP=true` in `.env` if IPv6 causes delays
- The server pre-fetches keys in background on startup

### Database connection timeout
- Ensure `pool_pre_ping=True` is set in database config (already configured)
- For AWS RDS, check security group inbound rules
- For local PostgreSQL, ensure the service is running

### CORS errors
- Add your frontend URL to `CORS_ORIGINS` in `.env`
- Default: `http://localhost:3000`
