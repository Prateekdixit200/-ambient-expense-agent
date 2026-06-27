import asyncio
import json
import os
from pathlib import Path
import vertexai
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from vertexai import types as vertex_types
from vertexai._genai.types.evals import AgentData
from expense_agent.agent import app as adk_app

# Decides manager approval decision based on whether the description contains prompt injection
def decide_approval(description: str) -> str:
    normalized = description.lower()
    injection_keywords = [
        "ignore previous", "ignore instructions", "bypass", "override", 
        "auto-approve", "auto approve", "system prompt", "developer instruction",
        "you must approve", "ignore rules", "override rules"
    ]
    if any(kw in normalized for kw in injection_keywords):
        return "reject"
    return "approve"

async def run_scenario(runner, session_service, case_id, payload_str):
    session = await session_service.create_session(
        session_id=case_id,
        user_id="test-user",
        app_name=adk_app.name
    )
    
    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part.from_text(text=payload_str)]
    )
    
    # Start execution
    events = runner.run_async(
        new_message=new_message,
        user_id="test-user",
        session_id=session.id
    )
    
    completed = False
    while not completed:
        paused_for_hitl = False
        interrupt_id = None
        
        async for event in events:
            # Check for manager review interrupt
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.function_call and part.function_call.name == "adk_request_input":
                        paused_for_hitl = True
                        interrupt_id = part.function_call.id
                        break
            if paused_for_hitl:
                break
                
        if paused_for_hitl and interrupt_id:
            payload = json.loads(payload_str)
            description = payload.get("description", "")
            decision = decide_approval(description)
            print(f"[{case_id}] Intercepted manager review: decision={decision}")
            
            # Prepare resume response
            resume_part = genai_types.Part(
                function_response=genai_types.FunctionResponse(
                    id=interrupt_id,
                    name="adk_request_input",
                    response={"response": decision}
                )
            )
            resume_message = genai_types.Content(role="user", parts=[resume_part])
            
            # Resume running
            events = runner.run_async(
                session_id=session.id,
                user_id="test-user",
                new_message=resume_message
            )
        else:
            completed = True
            
    # Retrieve complete session with events
    updated_session = await session_service.get_session(
        session_id=session.id,
        user_id="test-user",
        app_name=adk_app.name
    )
    return updated_session

def to_vertex_events(session_events):
    eval_events = []
    for ev in session_events:
        if not ev.content:
            continue
        parts = []
        if ev.content.parts:
            for p in ev.content.parts:
                part_dict = {}
                if p.text is not None:
                    part_dict["text"] = p.text
                if p.function_call:
                    part_dict["function_call"] = {
                        "name": p.function_call.name,
                        "args": p.function_call.args,
                        "id": p.function_call.id
                    }
                if p.function_response:
                    part_dict["function_response"] = {
                        "name": p.function_response.name,
                        "response": p.function_response.response,
                        "id": p.function_response.id
                    }
                if part_dict:
                    parts.append(part_dict)
        if not parts:
            continue
        content_dict = {
            "role": ev.content.role or "model",
            "parts": parts
        }
        
        event_data = {
            "author": ev.author,
            "content": content_dict
        }
        eval_events.append(vertex_types.Event.model_validate(event_data))
    return eval_events


async def main():
    # Setup Vertex AI using environment
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    vertexai.init(project=project_id, location=location)
    
    # Load dataset
    dataset_path = Path("tests/eval/datasets/basic-dataset.json")
    print(f"Loading dataset from {dataset_path}...")
    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset_data = json.load(f)
    
    cases = dataset_data.get("eval_cases", [])
    print(f"Loaded {len(cases)} eval cases.")
    
    session_service = InMemorySessionService()
    runner = Runner(app=adk_app, session_service=session_service)
    
    eval_cases = []
    for case in cases:
        case_id = case["eval_case_id"]
        prompt_text = case["prompt"]["parts"][0]["text"]
        print(f"\n--- Running case: {case_id} ---")
        
        session = await run_scenario(runner, session_service, case_id, prompt_text)
        
        # Convert session events to vertex AI events
        eval_events = to_vertex_events(session.events)
        
        # Construct Turn and AgentData
        turn = {
            "turn_index": 0,
            "events": eval_events
        }
        agent_data = AgentData(turns=[turn])
        
        # Extract final response
        final_text = ""
        for ev in reversed(session.events):
            if ev.content and ev.content.parts:
                for p in ev.content.parts:
                    if p.text:
                        final_text = p.text
                        break
                if final_text:
                    break
                    
        responses = []
        if final_text:
            responses.append({
                "response": {
                    "role": "model",
                    "parts": [{"text": final_text}]
                }
            })
            
        eval_case = vertex_types.EvalCase(
            eval_case_id=case_id,
            prompt={
                "role": "user",
                "parts": [{"text": prompt_text}]
            },
            agent_data=agent_data,
            responses=responses
        )
        eval_cases.append(eval_case)
        print(f"Finished {case_id}. Final response: {final_text}")
        
    dataset = vertex_types.EvaluationDataset(eval_cases=eval_cases)
    
    output_path = Path("artifacts/traces/generated_traces.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    output_path.write_text(
        dataset.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8"
    )
    print(f"\nSuccessfully generated and saved traces to {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
