import os
import logging
import base64
import json
from fastapi import FastAPI, Request, HTTPException
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from expense_agent.agent import app as adk_app

# 1. Setup standard Python logging for console logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ambient-expense-web-service")

# 2. Telemetry: Disable OpenTelemetry and GenAI cloud tracing/uploads locally
os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] = "false"
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"

# Initialize FastAPI app
fastapi_app = FastAPI(title="Ambient Expense Approval Service")

# Initialize ADK Runner
session_service = InMemorySessionService()
runner = Runner(
    app=adk_app,
    session_service=session_service,
)

@fastapi_app.post("/")
@fastapi_app.post("/pubsub")
@fastapi_app.post("/apps/expense_agent/trigger/pubsub")
async def handle_pubsub(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse request JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    logger.info(f"Received Pub/Sub message body: {body}")
    
    # 3. Normalize subscription path to short name and append message ID for uniqueness
    sub_path = body.get("subscription", "")
    if sub_path and "subscriptions/" in sub_path:
        sub_name = sub_path.split("subscriptions/")[-1]
    elif sub_path:
        sub_name = sub_path.split("/")[-1]
    else:
        sub_name = "ambient-expense-session"

    # Extract Pub/Sub message ID or use a timestamp to ensure unique session IDs
    message_dict = body.get("message") or {}
    message_id = message_dict.get("messageId")
    if not message_id:
        import time
        message_id = str(int(time.time() * 1000))
        
    session_id = f"{sub_name}-{message_id}"

    logger.info(f"Normalized session ID: {session_id}")

    # Serialize body to pass to the agent workflow parser
    raw_payload_str = json.dumps(body)
    user_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=raw_payload_str)]
    )

    try:
        # Create session if it does not exist
        try:
            session = await session_service.get_session(session_id=session_id, app_name=adk_app.name)
        except Exception:
            session = await session_service.create_session(
                session_id=session_id,
                user_id="pubsub-trigger",
                app_name=adk_app.name
            )

        events = runner.run_async(
            new_message=user_message,
            user_id="pubsub-trigger",
            session_id=session.id
        )

        response_chunks = []
        async for event in events:
            # Check if workflow paused on a Human-in-the-Loop task
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.function_call and part.function_call.name == "adk_request_input":
                        args = part.function_call.args
                        interrupt_id = args.get("interruptId")
                        msg = args.get("message")
                        logger.info(f"Workflow paused at HITL interrupt '{interrupt_id}': {msg}")
                        return {
                            "status": "paused",
                            "session_id": session_id,
                            "interrupt_id": interrupt_id,
                            "message": msg
                        }
                    elif part.text:
                        response_chunks.append(part.text)
            
            if isinstance(event.output, str):
                logger.info(f"Node output: {event.output}")

        full_response = "\n".join(response_chunks)
        logger.info(f"Workflow completed successfully: {full_response}")
        return {
            "status": "completed",
            "session_id": session_id,
            "response": full_response
        }

    except Exception as e:
        logger.exception(f"Error executing agent workflow: {e}")
        raise HTTPException(status_code=500, detail=str(e))
