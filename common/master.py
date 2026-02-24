from fastapi import FastAPI
import httpx

app = FastAPI()

EKS_AGENT_URL = "http://localhost:8081/"

@app.post("/thread")
async def handle_thread(payload: dict):

    rpc_payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tasks/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [
                    {"type": "text", "text": payload["question"]}
                ]
            },
            "metadata": payload["context"]
        }
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(EKS_AGENT_URL, json=rpc_payload)

    return response.json()