# 💳 Ambient Expense Agent

> AI-powered corporate expense approval system built with **Google Agent Development Kit (ADK) 2.0** using the **Graph Workflow API**.

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Google ADK](https://img.shields.io/badge/Google-ADK%202.0-green.svg)
![License](https://img.shields.io/badge/License-Apache%202.0-orange.svg)

---

## 🚀 Overview

Ambient Expense Agent automates corporate expense approvals by combining deterministic workflows with AI-assisted decision making.

The agent:

- ✅ Automatically approves low-value expenses
- 🔒 Performs security validation for high-value requests
- 🤖 Uses AI for risk assessment
- 👤 Supports human-in-the-loop approval
- 📊 Ready for evaluation and deployment with Google ADK 2.0

---

## 🏗 Workflow

```mermaid
flowchart TD
    A([Start]) --> B[ingest_expense]

    B --> C[check_threshold]

    C -- amount < $100 --> D[auto_approve]
    D --> E[book_expense]

    C -- amount ≥ $100 --> F[security_checkpoint]

    F -- Clean --> G[risk_reviewer]
    F -- Security Alert --> H[human_approval]

    G --> H

    H -- Approved --> E
    H -- Rejected --> I[reject_expense]
```

---

## 🛠 Tech Stack

- Python 3.11+
- Google ADK 2.0
- Graph Workflow API
- Google Agents CLI
- Gemini API
- uv
- Pytest

---

## ⚡ Quick Start

```bash
git clone https://github.com/Prateekdixit200/ambient-expense-agent.git

cd ambient-expense-agent

uv sync

agents-cli install

agents-cli playground
```

---

## 🔑 Environment

Create a `.env` file.

```env
GEMINI_API_KEY=YOUR_API_KEY
```

---

## 📂 Project Structure

```
ambient-expense-agent/

├── expense_agent/
├── deployment/
├── tests/
├── artifacts/
├── README.md
└── pyproject.toml
```

---

## ✨ Highlights

- Google ADK 2.0 Graph Workflow
- Human-in-the-loop Approval
- AI-assisted Risk Review
- Security Checkpoint
- Production-ready Workflow
- Local Evaluation Support
- Google Agent Runtime Deployment

---

## 📄 License

Licensed under the Apache License 2.0.
