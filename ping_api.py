"""Quick API key + connectivity check. Sends a 5-token call."""
import os
import anthropic

print(f"API key prefix: {os.environ.get('ANTHROPIC_API_KEY', '')[:15]}...")
print(f"Anthropic SDK version: {anthropic.__version__}")

client = anthropic.Anthropic()
resp = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=10,
    messages=[{"role": "user", "content": "Say 'ok'"}]
)
print(f"Response: {resp.content[0].text}")
print(f"Tokens used: input={resp.usage.input_tokens} output={resp.usage.output_tokens}")
