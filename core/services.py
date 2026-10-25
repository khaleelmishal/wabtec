import os
from django.conf import settings
from groq import Groq

def get_groq_client():
    """Initialize and return the Groq client using settings or environment variables."""
    api_key = getattr(settings, "GROQ_API_KEY", None) or os.getenv("GROQ_API_KEY")
    return Groq(api_key=api_key)

def ask_groq(prompt, system_prompt="You are a helpful industrial plant assistant.", model="qwen/qwen3.8-27b"):
    """Send a prompt to Groq and return the text response."""
    client = get_groq_client()
    
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=500,  # Limits the response output to stay under the 1000 OTPM free tier limit
    )
    return completion.choices[0].message.content