#!/usr/bin/env python3
"""A small, readable insurance claims SOP demo.

The important design choice is that the LLM is never the workflow controller.
The AgentSession owns phase transitions, verification, claim selection, and
allowed actions. An optional model may polish a response after those checks.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent
DATA = ROOT / "data"


def load_json(filename: str) -> Any:
    with (DATA / filename).open(encoding="utf-8") as file:
        return json.load(file)


POLICYHOLDERS = load_json("policyholders.json")
CLAIMS = load_json("claims.json")
GUIDELINE = load_json("required_document_guideline.json")


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def money(value: str) -> str:
    return f"${float(value):,.2f}"


def words_match(text: str, options: list[str]) -> bool:
    lowered = text.lower()
    return any(option in lowered for option in options)


class ClaimTools:
    """Read-only, grounded tools used by the agent after verification."""

    @staticmethod
    def claims_for_party(party_id: str) -> list[dict[str, Any]]:
        return [claim for claim in CLAIMS if claim["party_id"] == party_id]

    @staticmethod
    def find_claim(party_id: str, hint: dict[str, Any]) -> dict[str, Any] | None:
        claims = ClaimTools.claims_for_party(party_id)
        claim_id = hint.get("claim_id")
        if claim_id:
            return next((c for c in claims if c["case_id"].lower() == claim_id.lower()), None)

        candidates = claims
        case_type = hint.get("case_type")
        if case_type:
            candidates = [c for c in candidates if c["case_type"] == case_type] or candidates
        if hint.get("status"):
            status_candidates = [c for c in candidates if c["status"] == hint["status"]]
            candidates = status_candidates or candidates
        if hint.get("month"):
            month_candidates = [c for c in candidates if c["created_at"][5:7] == hint["month"]]
            candidates = month_candidates or candidates
        return candidates[0] if len(candidates) == 1 else None


class AgentSession:
    PII_FIELDS = ("name", "dob", "phone", "email", "ssn_last4")

    def __init__(self) -> None:
        self.phase = "VERIFY_ID"
        self.verified = False
        self.identity: dict[str, str] = {}
        self.memory: dict[str, Any] = {}
        self.selected_claim: dict[str, Any] | None = None
        self.irrelevant_attempts = 0
        self.email_choice: str | None = None
        self.history: list[dict[str, str]] = []

    def receive(self, message: str) -> str:
        self.history.append({"role": "user", "content": message})
        self.extract_memory(message)

        if self.is_out_of_scope(message):
            response = self.out_of_scope_response()
        elif self.phase == "VERIFY_ID":
            response = self.handle_verification(message)
        elif self.phase == "RESOLVE_INTENT":
            response = self.handle_intent(message)
        elif self.phase == "PROCESS_CASE":
            response = self.handle_case(message)
        else:
            response = self.handle_post_process(message)

        response = polish_with_model(message, response, self.safe_context())
        self.history.append({"role": "assistant", "content": response})
        return response

    def extract_memory(self, message: str) -> None:
        """Collect useful hints even when they appear before verification."""
        patterns = {
            "policy_number": r"\bPOL[- ]?\d{4}\b",
            "dob": r"\b(?:dob|date of birth|born)\D{0,12}(\d{4}[-/]\d{2}[-/]\d{2})\b",
            "email": r"\b[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}\b",
            "ssn_last4": r"\b(?:ssn|social security)\D{0,20}(?:last four|last 4|ending in)?\D{0,8}(\d{4})\b",
            "claim_id": r"\bCL[- ]?\d{4}\b",
        }
        for field, pattern in patterns.items():
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                value = match.group(1) if match.lastindex else match.group(0)
                self.memory[field] = value.replace(" ", "-").upper() if field in {"policy_number", "claim_id"} else value

        if re.search(r"\b(denied|rejected|declined)\b", message, re.I):
            self.memory["status"] = "denied"
        if re.search(r"\b(healthcare|medical|hospital|doctor|clinic)\b", message, re.I):
            self.memory["case_type"] = "healthcare"
        if re.search(r"\b(january|jan)\b", message, re.I):
            self.memory["month"] = "01"
        if re.search(r"\b(dental|dentist)\b", message, re.I):
            self.memory["case_type"] = "dental"
        if re.search(r"\b(auto|car|vehicle|collision|accident)\b", message, re.I):
            self.memory["case_type"] = "auto"

        name_match = re.search(r"(?i:my name is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})(?=\s*(?:,|\.|$|\band\b))", message)
        if not name_match:
            name_match = re.search(r"(?i:i am|i'm)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})(?=\s*(?:,|\.|$))", message)
        if not name_match:
            name_match = re.search(r"^\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})(?=\s*,)", message)
        if name_match:
            self.identity["name"] = name_match.group(1).strip()

        phone_match = re.search(r"(?:phone|number|call me at)\D{0,10}(\+?\d[\d ()-]{7,}\d)", message, re.I)
        if phone_match:
            self.identity["phone"] = re.sub(r"\D", "", phone_match.group(1))
        if "email" in self.memory:
            self.identity["email"] = self.memory["email"]
        for field in ("dob", "ssn_last4"):
            if field in self.memory:
                self.identity[field] = self.memory[field]

    def matching_fields(self) -> list[str]:
        policy_number = self.memory.get("policy_number")
        person = next((p for p in POLICYHOLDERS if policy_number and p["policy_number"] == policy_number), None)
        if not person and self.identity.get("name"):
            person = next((p for p in POLICYHOLDERS if normalize(p["name"]) == normalize(self.identity["name"])), None)
        if not person:
            return []

        matches: list[str] = []
        aliases = {"name": person.get("name_aliases", []) + [person["name"]],
                   "phone": person.get("phone_aliases", []) + [person["phone"]],
                   "email": person.get("email_aliases", []) + [person["email"]]}
        for field in self.PII_FIELDS:
            supplied = self.identity.get(field)
            expected = person.get("id_last4") if field == "ssn_last4" else person.get(field)
            if supplied and expected and (normalize(supplied) == normalize(str(expected)) or any(normalize(supplied) == normalize(a) for a in aliases.get(field, []))):
                matches.append(field)
        return matches

    def handle_verification(self, message: str) -> str:
        if self.verified:
            return "Your identity is already verified. What would you like to know about the claim?"
        matched = self.matching_fields()
        if len(matched) >= 3:
            person = next(p for p in POLICYHOLDERS if p["party_id"] == self.party_id())
            self.verified = True
            self.phase = "RESOLVE_INTENT"
            self.memory["party_id"] = person["party_id"]
            claim = ClaimTools.find_claim(person["party_id"], self.memory)
            if claim:
                self.selected_claim = claim
                self.phase = "PROCESS_CASE"
                return f"Thanks, I verified your identity using {len(matched)} details. I found the claim that matches what you described. What would you like to know about it?"
            return "Thanks, I verified your identity. I remembered that you had a claim question, so I’ll help narrow down which claim you mean."

        if self.is_frustrated(message):
            return "I understand this feels repetitive. Claim details are protected, so I need three matching details before I can discuss the claim. You can provide your full name, date of birth, phone, email, or the last four digits of your SSN."
        missing = 3 - len(matched)
        return f"I can help with that. Before discussing claim details, I need {missing} more matching identity detail{'' if missing == 1 else 's'}. You may use your full name, date of birth, phone, email, or SSN last four digits."

    def party_id(self) -> str | None:
        policy = self.memory.get("policy_number")
        person = next((p for p in POLICYHOLDERS if p["policy_number"] == policy), None)
        return person["party_id"] if person else None

    def handle_intent(self, message: str) -> str:
        self.memory.update(self.classify_intent(message))
        claim = ClaimTools.find_claim(self.party_id(), self.memory)
        if claim:
            self.selected_claim = claim
            self.phase = "PROCESS_CASE"
            if self.memory.get("intent") == "denial_question":
                return self.grounded_answer(claim, message)
            return f"I found claim {claim['case_id']} from {claim['created_at']}. What would you like to check about it?"
        return "I’m ready to help. Are you asking about claim status, a denial reason, submitting documents, or next steps?"

    def classify_intent(self, message: str) -> dict[str, str]:
        lowered = message.lower()
        if words_match(lowered, ["denied", "why rejected", "reason"]):
            return {"intent": "denial_question", "status": "denied"}
        if words_match(lowered, ["submit", "upload", "send documents", "documents"]):
            return {"intent": "document_submission"}
        if words_match(lowered, ["status", "where is my claim", "still pending"]):
            return {"intent": "status_inquiry"}
        return {"intent": "general_claim_question"}

    def handle_case(self, message: str) -> str:
        if not self.selected_claim:
            self.phase = "RESOLVE_INTENT"
            return self.handle_intent(message)
        self.memory.update(self.classify_intent(message))
        claim = self.selected_claim
        response = self.grounded_answer(claim, message)
        if words_match(message.lower(), ["thanks", "thank you", "that is all", "no more"]):
            self.phase = "POST_PROCESS"
        return response

    def grounded_answer(self, claim: dict[str, Any], message: str) -> str:
        lowered = message.lower()
        if claim["status"] == "denied" and words_match(lowered, ["why", "reason", "denied", "rejected"]):
            docs = ", ".join(claim.get("documents_needed", []))
            return f"Claim {claim['case_id']} is denied because {claim['denial_reason']}. The missing items are {docs}. The appeal deadline shown in the file is {claim['appeal_deadline']}."
        if words_match(lowered, ["submit", "upload", "send", "documents", "how do i"]):
            docs = ", ".join(claim.get("documents_needed", [])) or "the requested claim documents"
            guidance = GUIDELINE["default_guidance"]["en"]
            return f"For claim {claim['case_id']}, please submit {docs} through the member portal or claim upload link. {guidance}"
        if words_match(lowered, ["how long", "how soon", "processing time", "when"]):
            return f"Once the missing files for claim {claim['case_id']} are received and readable, review usually takes less than a week, although another review cycle may be needed."
        if words_match(lowered, ["amount", "paid", "payment", "reimbursement"]):
            return f"The grounded record for {claim['case_id']} shows expected reimbursement {money(claim['expected_reimbursement_amount'])}, allowed maximum {money(claim['allowed_max_amount'])}, and finalized payment {money(claim['net_pay'])}."
        return f"Claim {claim['case_id']} is currently {claim['status']}. The record describes it as: {claim['summary']}."

    def handle_post_process(self, message: str) -> str:
        lowered = message.lower()
        if self.email_choice is None:
            if words_match(lowered, ["yes", "send", "email"]):
                self.email_choice = "send"
                return "I’ll prepare an email summary with what we discussed, the claim outcome, and the next steps. In this demo, it is recorded as ready to send."
            if words_match(lowered, ["no", "skip", "don't", "do not"]):
                self.email_choice = "skip"
                return "No problem—I’ll skip the email summary. Is there anything else you need?"
            return "Would you like me to send an email summary of what we discussed, the claim outcome, and the next steps? You can say yes or no."
        return "The case conversation is complete. A human representative can help with anything outside this supported claims workflow."

    def is_frustrated(self, message: str) -> bool:
        return words_match(message.lower(), ["ridiculous", "angry", "frustrated", "already told", "waste of time", "just tell me"])

    def is_out_of_scope(self, message: str) -> bool:
        lowered = message.lower()
        if self.phase == "POST_PROCESS":
            return False
        insurance_words = ["claim", "policy", "insurance", "deductible", "coverage", "document", "appeal", "payment", "denied", "verify", "identity", "email", "status", "dob", "birth", "ssn", "social", "name", "phone"]
        return not any(word in lowered for word in insurance_words) and len(lowered.split()) > 1

    def out_of_scope_response(self) -> str:
        self.irrelevant_attempts += 1
        if self.irrelevant_attempts >= 3:
            return "That question is outside this claims-support workflow. I’m going to stop guessing and recommend a human representative. I can still help with identity verification, claim status, denial reasons, documents, or next steps."
        return "I can help with your insurance claim, policy, verification, documents, status, or next steps, but I can’t answer unrelated questions."

    def safe_context(self) -> dict[str, Any]:
        return {"phase": self.phase, "verified": self.verified, "claim_id": self.selected_claim.get("case_id") if self.selected_claim else None}


def polish_with_model(user_message: str, deterministic: str, context: dict[str, Any]) -> str:
    """Optionally polish style; never let the model alter protected facts."""
    token = os.getenv("AI_API_TOKEN") or os.getenv("OPENAI_API_KEY")
    if not token:
        return deterministic
    payload = {
        "model": os.getenv("AI_MODEL", "gpt-4o-mini"),
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "Rewrite the draft naturally in one or two concise sentences. Preserve every fact and instruction exactly. Do not add facts, claim details, or workflow actions. If the draft asks for verification, keep that requirement."},
            {"role": "user", "content": json.dumps({"phase": context, "customer_message": user_message, "draft": deterministic})},
        ],
    }
    request = urllib.request.Request(
        os.getenv("AI_API_URL", "https://api.openai.com/v1/chat/completions"),
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode())
        text = result["choices"][0]["message"]["content"].strip()
        return text if text else deterministic
    except Exception:
        return deterministic


class Handler(BaseHTTPRequestHandler):
    sessions: dict[str, AgentSession] = {}

    def send_json(self, body: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self.send_json({"ok": True})
            return
        if self.path != "/":
            self.send_error(404)
            return
        html = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON"}, 400)
            return
        session_id = str(payload.get("session_id", "demo"))
        if self.path == "/api/reset":
            self.sessions[session_id] = AgentSession()
            self.send_json({"ok": True, "phase": "VERIFY_ID"})
            return
        if self.path != "/api/chat" or not str(payload.get("message", "")).strip():
            self.send_json({"error": "A non-empty message is required"}, 400)
            return
        session = self.sessions.setdefault(session_id, AgentSession())
        reply = session.receive(str(payload["message"]).strip())
        self.send_json({"reply": reply, "phase": session.phase, "verified": session.verified, "claim_id": session.selected_claim.get("case_id") if session.selected_claim else None})


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    print(f"Insurance SOP harness running at http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
