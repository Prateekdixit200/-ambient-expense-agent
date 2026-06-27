# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import base64
import json
import re
from typing import Any
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from google.adk.workflow import Workflow, node, START
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.apps import App, ResumabilityConfig
from google.genai import types

from . import config

# Load environment variables
load_dotenv()

# Setup Local Authentication conditionally to prevent hangs if GCP credentials are not set up locally.
use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() in ("true", "1")
if use_vertex:
    import google.auth
    try:
        _, project_id = google.auth.default()
        if project_id:
            os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
    except Exception:
        pass
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-east1")


# Helper parser to extract expense payload from various Pub/Sub structures and encodings
def parse_input_event(node_input: Any) -> dict:
    text = ""
    if isinstance(node_input, types.Content):
        if node_input.parts:
            for part in node_input.parts:
                if part.text:
                    text = part.text
                    break
    elif isinstance(node_input, str):
        text = node_input
    
    if not text:
        return {}

    try:
        event_dict = json.loads(text)
    except Exception:
        return {}

    # Extract the payload key. Pub/Sub standard wraps details in "data" 
    # under the root or a nested "message" dictionary.
    data_val = event_dict.get("data")
    if data_val is None:
        message_dict = event_dict.get("message")
        if isinstance(message_dict, dict):
            data_val = message_dict.get("data")
            
    if data_val is None:
        # Fallback: if no data key exists, treat root JSON itself as the payload
        return event_dict

    # Decode payload
    if isinstance(data_val, str):
        try:
            # Try base64 decoding (standard for Pub/Sub)
            decoded = base64.b64decode(data_val).decode('utf-8')
            return json.loads(decoded)
        except Exception:
            # Fallback to plain JSON string parsing
            try:
                return json.loads(data_val)
            except Exception:
                return {}
    elif isinstance(data_val, dict):
        # Already parsed plain JSON
        return data_val
        
    return {}


# Schemas
class ExpenseReport(BaseModel):
    amount: float = Field(default=0.0, description="Amount of the expense in dollars.")
    submitter: str = Field(default="Unknown", description="Name of the person submitting the expense.")
    category: str = Field(default="Uncategorized", description="Category of the expense (e.g., travel, food, software).")
    description: str = Field(default="", description="Description of the expense.")
    date: str = Field(default="", description="The date of the transaction.")


class RiskAssessment(BaseModel):
    has_risk: bool = Field(description="True if there are any suspicious, high-risk, or policy-violating factors.")
    explanation: str = Field(description="Explanation of risk factors identified or why it is low risk.")


# ---------------------------------------------------------------------------
# ADK 2.0 Graph Workflow Nodes
# ---------------------------------------------------------------------------

def ingest_expense(node_input: Any) -> Event:
    data_dict = parse_input_event(node_input)
    expense = ExpenseReport(
        amount=float(data_dict.get("amount", 0.0)),
        submitter=str(data_dict.get("submitter", "Unknown")),
        category=str(data_dict.get("category", "Uncategorized")),
        description=str(data_dict.get("description", "")),
        date=str(data_dict.get("date", ""))
    )
    # Output the structured expense details and cache it in the workflow session state
    return Event(output=expense.model_dump(), state={"expense": expense.model_dump()})


# Regular expression patterns for PII scrubbing
SSN_PATTERN = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')
CC_PATTERN = re.compile(r'\b(?:\d[ -]*?){13,19}\b')

# Keyword list for prompt injection detection
INJECTION_KEYWORDS = [
    "ignore previous", "ignore instructions", "bypass", "override", 
    "auto-approve", "auto approve", "system prompt", "developer instruction",
    "you must approve", "ignore rules", "override rules"
]


def scrub_pii(text: str) -> tuple[str, list[str]]:
    redacted_categories = []
    cleaned_text = text
    
    if SSN_PATTERN.search(cleaned_text):
        cleaned_text = SSN_PATTERN.sub("[REDACTED SSN]", cleaned_text)
        redacted_categories.append("SSN")
        
    if CC_PATTERN.search(cleaned_text):
        cleaned_text = CC_PATTERN.sub("[REDACTED CREDIT CARD]", cleaned_text)
        redacted_categories.append("Credit Card")
        
    return cleaned_text, redacted_categories


def detect_injection(text: str) -> bool:
    normalized = text.lower()
    for kw in INJECTION_KEYWORDS:
        if kw in normalized:
            return True
    return False


def check_threshold(node_input: dict) -> Event:
    amount = node_input.get("amount", 0.0)
    if amount < config.THRESHOLD:
        return Event(output=node_input, route="auto_approve")
    return Event(output=node_input, route="needs_review")


def security_checkpoint(ctx: Context, node_input: dict) -> Event:
    expense = ctx.state.get("expense") or node_input
    desc = expense.get("description", "")
    
    # 1. Scrub personal PII data
    cleaned_desc, redacted_cats = scrub_pii(desc)
    
    # Update the expense payload inside state so the model and human see the clean version
    expense["description"] = cleaned_desc
    ctx.state["expense"] = expense
    
    # 2. Check for Prompt Injection
    has_injection = detect_injection(cleaned_desc)
    
    if has_injection:
        security_assessment = {
            "has_risk": True,
            "explanation": f"SECURITY ALERT: Potential prompt injection attempt detected in description. Redacted categories: {', '.join(redacted_cats) or 'None'}."
        }
        return Event(
            output=security_assessment,
            route="security_alert",
            state={"risk_assessment": security_assessment}
        )
        
    # Clean flow: proceed to model review
    if redacted_cats:
        ctx.state["redacted_categories"] = redacted_cats
        
    return Event(
        output=expense,
        route="clean"
    )


# LLM Risk reviewer node
risk_reviewer = LlmAgent(
    name="risk_reviewer",
    model=config.MODEL_NAME,
    instruction="""You are an automated corporate expense risk auditor.
Analyze the provided expense details (amount, submitter, category, description, and date).
Identify any suspicious signs, policy violations, or potential fraud, and produce a structured judgment.""",
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


@node(rerun_on_resume=True)
async def human_approval(ctx: Context, node_input: dict):
    # node_input holds the risk assessment (either from risk_reviewer or security_checkpoint)
    expense = ctx.state.get("expense") or {}
    
    # Check if human response has been received
    if not ctx.resume_inputs or "approve" not in ctx.resume_inputs:
        msg = (
            f"ALERT: Expense of ${expense.get('amount', 0.0):.2f} submitted by {expense.get('submitter', 'Unknown')} "
            f"({expense.get('category', 'Uncategorized')}: {expense.get('description', '')}) requires manager review.\n"
            f"Risk Assessment: Has Risk={node_input.get('has_risk', False)}, Explanation='{node_input.get('explanation', '')}'\n"
            "Do you approve or reject this expense? (approve/reject)"
        )
        yield RequestInput(
            interrupt_id="approve",
            message=msg
        )
        return

    val = ctx.resume_inputs["approve"]
    if isinstance(val, dict):
        val = val.get("response", "")
    val = str(val).strip().lower()
    
    if val in ("yes", "y", "approve", "approved"):
        yield Event(output=node_input, route="approved")
    else:
        yield Event(output=node_input, route="rejected")


def auto_approve(node_input: dict) -> Event:
    amount = node_input.get("amount", 0.0)
    desc = node_input.get("description", "")
    msg = f"Expense of ${amount:.2f} for '{desc}' auto-approved (under threshold)."
    return Event(
        output=node_input,
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)])
    )


def book_expense(ctx: Context, node_input: dict) -> Event:
    expense = ctx.state.get("expense") or node_input
    amount = expense.get("amount", 0.0)
    desc = expense.get("description", "")
    submitter = expense.get("submitter", "Unknown")
    msg = f"Expense of ${amount:.2f} for '{desc}' submitted by {submitter} has been successfully booked!"
    return Event(
        output=msg,
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)])
    )


def reject_expense(ctx: Context, node_input: dict) -> Event:
    expense = ctx.state.get("expense") or {}
    amount = expense.get("amount", 0.0)
    desc = expense.get("description", "")
    submitter = expense.get("submitter", "Unknown")
    msg = f"Expense of ${amount:.2f} for '{desc}' submitted by {submitter} was rejected."
    return Event(
        output=msg,
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)])
    )


# Workflow definition
root_agent = Workflow(
    name="root_agent",
    edges=[
        ('START', ingest_expense),
        (ingest_expense, check_threshold),
        # Routes from check_threshold
        (check_threshold, {
            "auto_approve": auto_approve,
            "needs_review": security_checkpoint
        }),
        # Routes from security_checkpoint
        (security_checkpoint, {
            "clean": risk_reviewer,
            "security_alert": human_approval
        }),
        # Auto approve flow
        (auto_approve, book_expense),
        # Risk reviewer flow
        (risk_reviewer, human_approval),
        # Human approval flow
        (human_approval, {
            "approved": book_expense,
            "rejected": reject_expense
        }),
    ]
)


app = App(
    root_agent=root_agent,
    name="expense_agent",
    resumability_config=ResumabilityConfig(is_resumable=True)
)
