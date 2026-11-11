# Insurance Claims SOP Harness

This is a small, dependency-free demo of a conversational insurance claims support agent. It keeps the business workflow in code and uses an optional AI model only for bounded conversational phrasing.

## Live Demo

[Insurance Claims SOP Agent](https://project-exud.onrender.com)
The demo supports identity verification, intent resolution, grounded claim processing, and post-case email-summary consent.

## What is included

- `server.py` — HTTP server, workflow state machine, grounded claim tools, and optional model client.
- `static/index.html` — simple chat UI for testing the complete workflow.
- `data/` — the supplied starter JSON fixtures.
- `tests/test_agent.py` — basic workflow and safety tests.

The workflow is always:

`VERIFY_ID -> RESOLVE_INTENT -> PROCESS_CASE -> POST_PROCESS`

Claim details are never returned before at least three matching PII fields have been verified. The policy number is collected as a useful lookup hint but is not counted as one of the three PII fields.

## Run locally

The demo uses only the Python standard library, so installed dependencies are not needed.

```bash
cd insurance-sop-harness
python3 server.py
```

Open <http://localhost:8000>.

To enable optional AI phrasing, set an OpenAI-compatible token and endpoint before starting the server:

```bash
export AI_API_TOKEN="your-token"
export AI_MODEL="gpt-4o-mini"
# Optional; defaults to the OpenAI endpoint.
export AI_API_URL="https://api.openai.com/v1/chat/completions"
python3 server.py
```

The model receives only the current customer message, the current safe phase, and a compact set of already-grounded facts. It cannot change the phase, verify identity, select a claim, or add facts. If the API is unavailable, the deterministic response is used.

## Test the supplied scenario

In the UI, send:

> I’m the policyholder. My name is Margaret Chen, policy POL-9921. I’m calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.

The agent should verify Margaret with name, date of birth, and SSN last four, retain the denied-healthcare-January hint, and select claim `CL-2048` after verification. Try asking for the denial reason before verification in a new session; the agent must refuse to disclose it.

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

## Submission

Zip this project without `.venv`, `node_modules`, caches, or other installed dependencies. The project is intentionally self-contained and is under 50 MB.
