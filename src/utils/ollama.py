"""Authentication shared by Ollama generation and health probes."""
from src import config


def request_headers():
    headers = {"Content-Type": "application/json"}
    if config.OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {config.OLLAMA_API_KEY}"
    client_id = config.OLLAMA_ACCESS_CLIENT_ID
    secret = config.OLLAMA_ACCESS_CLIENT_SECRET
    if bool(client_id) != bool(secret):
        raise ValueError("Configure both Ollama Cloudflare Access credentials.")
    if client_id:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = secret
    return headers
